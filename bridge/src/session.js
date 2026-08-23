/** Owns the Baileys sockets: pairing, reconnects, sending, teardown. */

import fs from "node:fs/promises";
import path from "node:path";

import makeWASocket, {
	DisconnectReason,
	downloadMediaMessage,
	fetchLatestBaileysVersion,
	useMultiFileAuthState,
} from "@whiskeysockets/baileys";
import QRCode from "qrcode";

import { config } from "./config.js";
import { baileysLogger, logger } from "./logger.js";
import { isGroup, isVoice, normalise } from "./messages.js";
import { deliver } from "./webhook.js";

// Reconnect with a backoff so a server-side outage does not become a hot loop.
const RECONNECT_BASE_MS = 2000;
const RECONNECT_MAX_MS = 60000;

/** Session ids become directory names, so keep them boring. */
function assertSafeId(id) {
	if (!/^[A-Za-z0-9._-]{1,64}$/.test(String(id || ""))) {
		throw new Error("Session id must be 1-64 chars of letters, digits, dot, dash, or underscore");
	}
	return id;
}

export function toJid(number) {
	const raw = String(number || "").trim();
	if (!raw) throw new Error("A recipient is required");
	if (raw.includes("@")) return raw;

	const digits = raw.replace(/\D/g, "");
	if (!digits) throw new Error(`Not a usable recipient: ${number}`);
	return `${digits}@s.whatsapp.net`;
}

class Session {
	constructor(id, manager) {
		this.id = assertSafeId(id);
		this.manager = manager;
		this.dir = path.join(config.sessionDir, this.id);

		this.sock = null;
		this.state = "disconnected"; // disconnected | pairing | connected | logged_out
		this.qr = null; // data URL, valid until the next rotation
		this.qrExpiresAt = null;
		this.qrAttempts = 0;
		this.phone = null;
		this.lastError = null;
		this.reconnectAttempts = 0;
		this.reconnectTimer = null;
		this.closing = false;
	}

	get status() {
		return {
			session: this.id,
			state: this.state,
			phone: this.phone,
			has_qr: Boolean(this.qr),
			qr_expires_at: this.qrExpiresAt,
			last_error: this.lastError,
		};
	}

	async emit(event, data = {}) {
		await deliver({ event, session: this.id, ...data, at: new Date().toISOString() });
	}

	async connect() {
		if (this.sock) return this.status;

		this.closing = false;
		await fs.mkdir(this.dir, { recursive: true });

		const { state, saveCreds } = await useMultiFileAuthState(this.dir);
		const { version } = await fetchLatestBaileysVersion();

		logger.info({ session: this.id, version }, "starting socket");

		this.sock = makeWASocket({
			version,
			auth: state,
			logger: baileysLogger,
			// We render the QR ourselves and ship it to Frappe.
			printQRInTerminal: false,
			// Presence updates and receipts from every chat are noise we never read.
			markOnlineOnConnect: false,
			syncFullHistory: false,
			browser: ["AgentX", "Chrome", "1.0.0"],
		});

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

			const event = normalise(this.id, raw);
			// Status broadcasts are not conversations.
			if (event.chat_id === "status@broadcast") continue;

			// Decryption needs this message object, which we will not have later,
			// so a voice note is fetched now or not at all.
			if (!event.from_me && isVoice(event)) {
				event.media = { ...event.media, base64: await this.inlineAudio(raw, event) };
			}

			await this.emit("message", { message: event });
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

	list() {
		return [...this.sessions.values()].map((session) => session.status);
	}

	async start(id) {
		const session = this.get(id, { create: true });
		await session.connect();
		return session.status;
	}

	async remove(id) {
		const session = this.get(id);
		if (!session) return { session: id, state: "unknown" };

		await session.logout();
		this.sessions.delete(id);
		return { session: id, state: "removed" };
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
				await this.start(entry.name);
				restored.push(entry.name);
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
