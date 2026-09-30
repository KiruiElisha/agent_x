/** Owns the Baileys sockets: pairing, reconnects, sending, teardown. */

import fs from "node:fs/promises";
import path from "node:path";

import makeWASocket, {
	Browsers,
	DisconnectReason,
	downloadMediaMessage,
	fetchLatestBaileysVersion,
	useMultiFileAuthState,
} from "@whiskeysockets/baileys";
import QRCode from "qrcode";

import { config } from "./config.js";
import { baileysLogger, logger } from "./logger.js";
import { isConversation, isGroup, isVoice, normalise } from "./messages.js";
import { deliver } from "./webhook.js";

// Reconnect with a backoff so a server-side outage does not become a hot loop.
const RECONNECT_BASE_MS = 2000;
const RECONNECT_MAX_MS = 60000;

/** Session ids become directory names, so keep them boring. */
export function assertSafeId(id) {
	if (!/^[A-Za-z0-9._-]{1,120}$/.test(String(id || ""))) {
		throw new Error("Session id must be 1-120 chars of letters, digits, dot, dash, or underscore");
	}
	return id;
}

/** A tenant's sessions live beside the local ones, under a name nobody else can guess at. */
export function storageId(access, publicId) {
	assertSafeId(publicId);
	if (!access || access.role === "admin") return publicId;
	assertSafeId(access.id);
	return assertSafeId(`${access.id}--${publicId}`);
}

function httpError(status, message) {
	const error = new Error(message);
	error.statusCode = status;
	return error;
}

/** Baileys returns eight characters. WhatsApp shows them as two groups. */
export function formatPairingCode(code) {
	const raw = String(code || "").replace(/[^A-Za-z0-9]/g, "");
	if (raw.length === 8) return `${raw.slice(0, 4)}-${raw.slice(4)}`;
	return raw;
}

/** Baileys wants a Buffer for inline bytes, or {url} for a remote fetch. */
function toMediaUpload({ url, base64 }) {
	if (base64) return Buffer.from(base64, "base64");
	if (url) return { url };
	throw new Error("Each product image needs a url or base64 content");
}

async function baileysVersion() {
	try {
		const result = await Promise.race([
			fetchLatestBaileysVersion(),
			new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 8000)),
		]);
		return result.version;
	} catch (error) {
		logger.warn({ err: error.message }, "could not fetch Baileys version, using the library default");
		return undefined;
	}
}

export function toJid(number) {
	const raw = String(number || "").trim();
	if (!raw) throw new Error("A recipient is required");
	if (raw.includes("@")) return raw;

	const digits = raw.replace(/\D/g, "");
	if (!digits) throw new Error(`Not a usable recipient: ${number}`);
	return `${digits}@s.whatsapp.net`;
}

function waclientEvent(instanceId, event, data) {
	if (event === "message") {
		return {
			instance_id: instanceId,
			event: "messages.upsert",
			data: { event: "messages.upsert", messages: [data.raw] },
		};
	}
	if (event === "receipt") {
		return {
			instance_id: instanceId,
			event: "messages.update",
			data: {
				event: "messages.update",
				messages: [
					{
						key: { id: data.message_id, remoteJid: data.chat_id },
						update: { status: data.status },
					},
				],
			},
		};
	}
	return {
		instance_id: instanceId,
		event: "connection.update",
		data: {
			event: "connection.update",
			connection: event === "connected" ? "open" : "close",
			state: event,
		},
	};
}

class Session {
	constructor(id, manager) {
		this.id = assertSafeId(id);
		this.manager = manager;
		this.dir = path.join(config.sessionDir, this.id);

		this.publicId = id;
		this.tenantId = null;
		this.webhookUrl = "";
		this.webhookSecret = "";
		this.webhookStyle = "agentx";

		this.sock = null;
		this.state = "disconnected"; // disconnected | pairing | connected | logged_out
		this.qr = null; // data URL, valid until the next rotation
		this.qrExpiresAt = null;
		this.qrAttempts = 0;
		this.pairingCode = null;
		this.phone = null;
		this.lastError = null;
		this.reconnectAttempts = 0;
		this.reconnectTimer = null;
		this.closing = false;
	}

	get status() {
		return {
			session: this.publicId || this.id,
			state: this.state,
			phone: this.phone,
			tenant: this.tenantId,
			has_qr: Boolean(this.qr),
			qr_expires_at: this.qrExpiresAt,
			last_error: this.lastError,
		};
	}

	callback() {
		return { webhookUrl: this.webhookUrl, webhookSecret: this.webhookSecret };
	}

	async loadMeta() {
		try {
			const raw = await fs.readFile(path.join(this.dir, "meta.json"), "utf8");
			const meta = JSON.parse(raw);
			this.publicId = meta.public_id || this.publicId;
			this.tenantId = meta.tenant_id || this.tenantId;
			this.webhookUrl = meta.webhook_url || this.webhookUrl;
			this.webhookSecret = meta.webhook_secret || this.webhookSecret;
			this.webhookStyle = meta.webhook_style || this.webhookStyle || "agentx";
		} catch (error) {
			if (error.code !== "ENOENT") {
				logger.warn({ session: this.id, err: error.message }, "could not read session meta");
			}
		}
	}

	async saveMeta() {
		await fs.mkdir(this.dir, { recursive: true });
		const body = JSON.stringify(
			{
				public_id: this.publicId,
				tenant_id: this.tenantId,
				webhook_url: this.webhookUrl,
				webhook_secret: this.webhookSecret,
				webhook_style: this.webhookStyle || "agentx",
			},
			null,
			2,
		);
		await fs.writeFile(path.join(this.dir, "meta.json"), body);
	}

	/**
	 * Remember who owns the session and where its events go.
	 * A tenant cannot take a session that already belongs to someone else.
	 */
	async configure({ publicId, tenantId, webhookUrl, webhookSecret, webhookStyle } = {}) {
		await this.loadMeta();

		if (tenantId && this.tenantId && this.tenantId !== tenantId) {
			throw httpError(403, "session belongs to another tenant");
		}

		if (publicId) this.publicId = publicId;
		if (tenantId) this.tenantId = tenantId;
		if (webhookUrl) this.webhookUrl = String(webhookUrl);
		if (webhookSecret) this.webhookSecret = String(webhookSecret);
		if (webhookStyle) this.webhookStyle = webhookStyle;

		await this.saveMeta();
		return this.status;
	}

	waitFor(predicate, ms) {
		if (predicate()) return Promise.resolve(true);

		return new Promise((resolve) => {
			const poll = setInterval(() => {
				if (!predicate()) return;
				clearInterval(poll);
				clearTimeout(timer);
				resolve(true);
			}, 200);

			const timer = setTimeout(() => {
				clearInterval(poll);
				resolve(false);
			}, ms);
		});
	}

	async emit(event, data = {}) {
		if (this.webhookStyle === "waclient") {
			await deliver(waclientEvent(this.publicId || this.id, event, data), {
				...this.callback(),
				unsigned: true,
			});
			return;
		}
		await deliver(
			{ event, session: this.publicId || this.id, ...data, at: new Date().toISOString() },
			this.callback(),
		);
	}

	async connect() {
		if (this.sock) return this.status;

		this.closing = false;
		await fs.mkdir(this.dir, { recursive: true });

		const { state, saveCreds } = await useMultiFileAuthState(this.dir);
		const version = await baileysVersion();

		logger.info({ session: this.id, version: version || "default" }, "starting socket");

		const options = {
			auth: state,
			logger: baileysLogger,
			// Presence updates and receipts from every chat are noise we never read.
			markOnlineOnConnect: false,
			syncFullHistory: false,
			// A made-up browser name is rejected by WhatsApp: the QR scans and
			// the pairing code is accepted, then the link is dropped. A stock
			// desktop identity is what linked devices expect.
			browser: Browsers.ubuntu("Chrome"),
		};
		if (version) options.version = version;

		this.sock = makeWASocket(options);
		if (state.creds?.registered && this.pendingPair) {
			this.pairTask = Promise.reject(
				httpError(400, "This number is already linked. Press Connect."),
			);
			this.pairTask.catch(() => {});
		} else if (this.pendingPair) {
			this.pairTask = this.issuePairingCode(this.pendingPair);
		} else {
			this.pairTask = Promise.resolve(null);
		}

		this.sock.ev.on("creds.update", saveCreds);
		this.sock.ev.on("connection.update", (update) => {
			this.onConnectionUpdate(update).catch((error) =>
				logger.error({ session: this.id, err: error.message }, "connection update failed"),
			);
		});
		this.sock.ev.on("messages.upsert", (batch) => {
			this.onMessages(batch).catch((error) =>
				logger.error({ session: this.id, err: error.message }, "message handling failed"),
			);
		});
		this.sock.ev.on("messages.update", (updates) => {
			this.onReceipts(updates).catch((error) =>
				logger.error({ session: this.id, err: error.message }, "receipt handling failed"),
			);
		});

		return this.status;
	}

	async onConnectionUpdate({ connection, lastDisconnect, qr }) {
		if (qr) await this.onQr(qr);

		if (connection === "open") {
			this.state = "connected";
			this.qr = null;
			this.qrExpiresAt = null;
			this.qrAttempts = 0;
			this.reconnectAttempts = 0;
			this.lastError = null;
			this.phone = this.sock?.user?.id ? this.sock.user.id.split(":")[0].split("@")[0] : null;

			logger.info({ session: this.id, phone: this.phone }, "connected");
			await this.emit("connected", { phone: this.phone, name: this.sock?.user?.name || null });
			return;
		}

		if (connection === "close") await this.onClose(lastDisconnect);
	}

	async onQr(qr) {
		this.qrAttempts += 1;

		if (this.qrAttempts > config.maxQrRetries) {
			logger.warn({ session: this.id }, "qr not scanned in time, stopping pairing");
			this.lastError = "QR expired without being scanned";
			await this.stop();
			await this.emit("qr_expired", {});
			return;
		}

		this.state = "pairing";
		this.qr = await QRCode.toDataURL(qr, { margin: 1, width: 320 });
		// Baileys rotates roughly every 60s; Frappe uses this to grey out a stale code.
		this.qrExpiresAt = new Date(Date.now() + 60000).toISOString();

		logger.info({ session: this.id, attempt: this.qrAttempts }, "qr ready");
		await this.emit("qr", { qr: this.qr, expires_at: this.qrExpiresAt, attempt: this.qrAttempts });
	}

	async onClose(lastDisconnect) {
		const statusCode = lastDisconnect?.error?.output?.statusCode;
		const loggedOut = statusCode === DisconnectReason.loggedOut;

		this.sock = null;
		this.lastError = lastDisconnect?.error?.message || null;

		if (this.closing) {
			this.state = "disconnected";
			return;
		}

		if (loggedOut) {
			// The phone unlinked us. The stored credentials are dead weight and
			// would block a fresh pairing, so clear them.
			logger.warn({ session: this.id }, "logged out from the phone");
			this.state = "logged_out";
			this.qr = null;
			await this.clearCredentials();
			await this.emit("logged_out", { reason: this.lastError });
			return;
		}

		this.state = "disconnected";
		await this.scheduleReconnect(statusCode);
	}

	async scheduleReconnect(statusCode) {
		this.reconnectAttempts += 1;
		const delay = Math.min(
			RECONNECT_BASE_MS * 2 ** (this.reconnectAttempts - 1),
			RECONNECT_MAX_MS,
		);

		logger.info(
			{ session: this.id, statusCode, attempt: this.reconnectAttempts, delay },
			"reconnecting",
		);
		await this.emit("disconnected", { reason: this.lastError, retry_in_ms: delay });

		this.reconnectTimer = setTimeout(() => {
			this.reconnectTimer = null;
			this.connect().catch((error) =>
				logger.error({ session: this.id, err: error.message }, "reconnect failed"),
			);
		}, delay);
	}

	async onMessages({ messages, type }) {
		// "append" is history backfill; only "notify" is a live arrival.
		if (type !== "notify") return;

		for (const raw of messages || []) {
			if (!raw?.message) continue;

			const event = normalise(this.publicId || this.id, raw);
			// Statuses, channels, and broadcast lists are not conversations.
			if (!isConversation(event.chat_id)) continue;

			// Decryption needs this message object, which we will not have later,
			// so a voice note is fetched now or not at all.
			if (!event.from_me && isVoice(event)) {
				event.media = { ...event.media, base64: await this.inlineAudio(raw, event) };
			}

			await this.emit("message", this.webhookStyle === "waclient" ? { raw } : { message: event });
		}
	}

	async onReceipts(updates) {
		for (const update of updates || []) {
			const status = update.update?.status;
			if (status === undefined || status === null) continue;

			await this.emit("receipt", {
				message_id: update.key?.id || null,
				chat_id: update.key?.remoteJid || null,
				// Baileys ships an enum: 1 pending, 2 server ack, 3 delivered, 4 read, 5 played.
				status,
			});
		}
	}

	requireConnected() {
		if (this.state !== "connected" || !this.sock) {
			throw new Error(`Session ${this.id} is ${this.state}, not connected`);
		}
	}

	async sendText(to, text) {
		this.requireConnected();
		const jid = toJid(to);
		const result = await this.sock.sendMessage(jid, { text: String(text ?? "") });
		return { message_id: result?.key?.id || null, chat_id: jid };
	}

	async sendMedia(to, { url, base64, mimetype, filename, caption, kind }) {
		this.requireConnected();
		const jid = toJid(to);

		if (!url && !base64) throw new Error("Media needs either a url or base64 content");
		const source = base64 ? Buffer.from(base64, "base64") : { url };

		const payload = { caption: caption || undefined, mimetype: mimetype || undefined };

		switch (kind || "document") {
			case "image":
				payload.image = source;
				break;
			case "video":
				payload.video = source;
				break;
			case "audio":
				payload.audio = source;
				// Without this WhatsApp shows a file card instead of a player.
				payload.ptt = false;
				delete payload.caption;
				break;
			default:
				payload.document = source;
				payload.fileName = filename || "file";
		}

		const result = await this.sock.sendMessage(jid, payload);
		return { message_id: result?.key?.id || null, chat_id: jid };
	}

	/** Fetch a voice note now, while the message can still be decrypted. */
	async inlineAudio(raw, event) {
		const size = event.media?.size || 0;
		if (size && size > config.inlineAudioMaxBytes) {
			logger.info({ session: this.id, size }, "voice note too large to inline");
			return undefined;
		}

		try {
			const buffer = await downloadMediaMessage(
				raw,
				"buffer",
				{},
				{ logger: baileysLogger, reuploadRequest: this.sock.updateMediaMessage },
			);

			if (buffer.length > config.inlineAudioMaxBytes) return undefined;
			return buffer.toString("base64");
		} catch (error) {
			// A voice note we cannot fetch should still arrive as a message.
			logger.warn({ session: this.id, err: error.message }, "could not fetch voice note");
			return undefined;
		}
	}

	/** Re-download the bytes of a message we already received. */
	async downloadMedia(rawMessage) {
		this.requireConnected();
		const buffer = await downloadMediaMessage(
			rawMessage,
			"buffer",
			{},
			{ logger: baileysLogger, reuploadRequest: this.sock.updateMediaMessage },
		);
		return buffer.toString("base64");
	}

	/**
	 * Eight-character code typed on the phone, instead of scanning.
	 * WhatsApp only issues one in the first moments after the socket opens.
	 * Waiting for a QR first takes the scan path, and then no code comes back.
	 */
	async requestPairingCode(phone) {
		const digits = String(phone || "").replace(/\D/g, "");
		if (!digits) throw httpError(400, "A phone number in international format is required");

		if (this.state === "connected") {
			throw httpError(400, "This session is already connected");
		}

		this.pendingPair = digits;
		if (this.sock) {
			await this.stop();
			this.closing = false;
		}
		await this.connect();

		try {
			const code = await this.pairTask;
			if (!code) throw httpError(400, "WhatsApp did not return a pairing code");
			return { pairing_code: code, phone: digits, state: this.state };
		} finally {
			this.pendingPair = null;
		}
	}

	async issuePairingCode(digits) {
		// The socket needs a moment to finish its handshake before it will
		// accept a pairing-code request. Asking immediately is rejected.
		await new Promise((resolve) => setTimeout(resolve, 2500));
		if (!this.sock) throw httpError(400, "WhatsApp closed before a code was issued");
		if (this.state === "connected") throw httpError(400, "This session is already connected");

		const code = await this.sock.requestPairingCode(digits);
		this.pairingCode = formatPairingCode(code);
		this.state = "pairing";
		logger.info({ session: this.id }, "pairing code issued");
		await this.emit("pairing_code", { pairing_code: this.pairingCode, phone: digits });
		return this.pairingCode;
	}

	async getCatalog(jid, { limit, cursor } = {}) {
		this.requireConnected();
		return this.sock.getCatalog({ jid: jid || undefined, limit, cursor });
	}

	async getCollections(jid, limit) {
		this.requireConnected();
		return this.sock.getCollections(jid || undefined, limit);
	}

	async createProduct({ images, ...product }) {
		this.requireConnected();
		if (!Array.isArray(images) || !images.length) {
			throw new Error("At least one product image is required");
		}
		return this.sock.productCreate({ ...product, images: images.map(toMediaUpload) });
	}

	async updateProduct(productId, { images, ...update }) {
		this.requireConnected();
		const payload = images ? { ...update, images: images.map(toMediaUpload) } : update;
		return this.sock.productUpdate(productId, payload);
	}

	async deleteProducts(productIds) {
		this.requireConnected();
		return this.sock.productDelete(productIds);
	}

	async getOrderDetails(orderId, tokenBase64) {
		this.requireConnected();
		return this.sock.getOrderDetails(orderId, tokenBase64);
	}

	async checkNumber(number) {
		this.requireConnected();
		const jid = toJid(number);
		if (isGroup(jid)) return { exists: true, jid };

		const [result] = await this.sock.onWhatsApp(jid);
		return { exists: Boolean(result?.exists), jid: result?.jid || jid };
	}

	/** Close the socket but keep credentials, so connect() resumes silently. */
	async stop() {
		this.closing = true;

		if (this.reconnectTimer) {
			clearTimeout(this.reconnectTimer);
			this.reconnectTimer = null;
		}

		if (this.sock) {
			try {
				// end() tears down without telling WhatsApp to unlink.
				this.sock.end(undefined);
			} catch (error) {
				logger.debug({ session: this.id, err: error.message }, "socket end failed");
			}
			this.sock = null;
		}

		this.state = "disconnected";
		this.qr = null;
		return this.status;
	}

	/** Unlink from the phone and forget the credentials. */
	async logout() {
		if (this.sock && this.state === "connected") {
			try {
				await this.sock.logout();
			} catch (error) {
				logger.warn({ session: this.id, err: error.message }, "logout call failed");
			}
		}

		await this.stop();
		await this.clearCredentials();
		this.state = "logged_out";
		this.phone = null;

		await this.emit("logged_out", { reason: "requested" });
		return this.status;
	}

	async clearCredentials() {
		try {
			await fs.rm(this.dir, { recursive: true, force: true });
		} catch (error) {
			logger.error({ session: this.id, err: error.message }, "could not clear credentials");
		}
	}
}

export class SessionManager {
	constructor() {
		this.sessions = new Map();
	}

	get(id, { create = false } = {}) {
		assertSafeId(id);

		let session = this.sessions.get(id);
		if (!session) {
			if (!create) return null;
			session = new Session(id, this);
			this.sessions.set(id, session);
		}
		return session;
	}

	list(access) {
		return [...this.sessions.values()]
			.filter((session) => !access || access.role === "admin" || session.tenantId === access.id)
			.map((session) => session.status);
	}

	/**
	 * The session this caller is allowed to touch, or null.
	 * A tenant's public name `main` is stored as `<tenant>--main`.
	 */
	owned(publicId, access) {
		const session = this.sessions.get(storageId(access, publicId));
		if (!session) return null;
		if (access?.role === "tenant" && session.tenantId && session.tenantId !== access.id) return null;
		return session;
	}

	async open(publicId, access, options = {}) {
		const key = storageId(access, publicId);
		const session = this.get(key, { create: true });
		await session.configure({
			publicId,
			tenantId: access?.role === "tenant" ? access.id : undefined,
			webhookUrl: options.webhook_url,
			webhookSecret: options.webhook_secret,
			webhookStyle: options.webhook_style,
		});
		return session;
	}

	async start(publicId, access, options = {}) {
		const session = await this.open(publicId, access, options);
		await session.connect();
		return session;
	}

	async remove(publicId, access) {
		const session = this.owned(publicId, access);
		if (!session) return { session: publicId, state: "unknown" };

		await session.logout();
		this.sessions.delete(session.id);
		return { session: publicId, state: "removed" };
	}

	/** Bring back every session that still has credentials on disk. */
	async restoreAll() {
		let entries;
		try {
			entries = await fs.readdir(config.sessionDir, { withFileTypes: true });
		} catch {
			logger.info("no session directory yet, nothing to restore");
			return [];
		}

		const restored = [];
		for (const entry of entries) {
			if (!entry.isDirectory()) continue;

			// A directory without creds.json was never paired.
			try {
				await fs.access(path.join(config.sessionDir, entry.name, "creds.json"));
			} catch {
				continue;
			}

			try {
				const session = this.get(entry.name, { create: true });
				await session.loadMeta();
				await session.connect();
				restored.push(session.publicId || entry.name);
			} catch (error) {
				logger.error({ session: entry.name, err: error.message }, "could not restore session");
			}
		}

		logger.info({ restored }, "sessions restored");
		return restored;
	}

	async shutdown() {
		await Promise.all([...this.sessions.values()].map((session) => session.stop()));
	}
}
