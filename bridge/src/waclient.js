/** WaClient-shaped API.

Apps such as Queen already talk to api.waclient.com with an access token and
an instance id. Point that same client at this bridge and those two fields
still work. AgentX does not have to be installed on their site.
*/

import crypto from "node:crypto";
import express from "express";

import { resolveAccess } from "./tenants.js";

const STATES = {
	connected: "connected",
	pairing: "connecting",
	logged_out: "logged_out",
	disconnected: "disconnected",
};

function fields(req) {
	return { ...(req.query || {}), ...(req.body || {}) };
}

function ok(extra = {}) {
	return { status: "success", message: "Success", ...extra };
}

function fail(res, status, message) {
	return res.status(status).json({ status: "error", message });
}

async function caller(req, res) {
	const body = fields(req);
	const token = body.access_token || "";
	const access = token ? await resolveAccess(token) : null;
	if (!access) {
		fail(res, 401, "unauthorised");
		return null;
	}
	return { access, body };
}

function instanceId(body) {
	const id = String(body.instance_id || "").trim();
	if (!id) {
		const error = new Error("instance_id is required");
		error.statusCode = 400;
		throw error;
	}
	return id;
}

function connectionState(session) {
	const state = STATES[session?.state] || "disconnected";
	return {
		connection_state: state,
		status: state,
		relogin_required: session?.state === "logged_out",
		phone: session?.phone || null,
	};
}

export function buildWaClientRouter(manager) {
	const router = express.Router();

	router.post("/create_instance", async (req, res, next) => {
		try {
			const who = await caller(req, res);
			if (!who) return;
			const id = crypto.randomBytes(7).toString("hex").toUpperCase();
			await manager.open(id, who.access, { webhook_style: "waclient" });
			return res.json(ok({ instance_id: id }));
		} catch (error) {
			return next(error);
		}
	});

	router.post("/list_instances", async (req, res, next) => {
		try {
			const who = await caller(req, res);
			if (!who) return;
			const instances = manager.list(who.access).map((session) => ({
				instance_id: session.session,
				...connectionState(session),
			}));
			return res.json(ok({ data: instances }));
		} catch (error) {
			return next(error);
		}
	});

	const one = (handler) => async (req, res, next) => {
		try {
			const who = await caller(req, res);
			if (!who) return;
			const id = instanceId(who.body);
			return await handler(req, res, who, id);
		} catch (error) {
			return next(error);
		}
	};

	async function live(access, id) {
		let session = manager.owned(id, access);
		if (!session) session = await manager.open(id, access, { webhook_style: "waclient" });
		return session;
	}

	router.all("/instance_status", one(async (req, res, who, id) => {
		const session = manager.owned(id, who.access);
		return res.json(ok({ data: connectionState(session) }));
	}));

	router.all("/instance_info", one(async (req, res, who, id) => {
		const session = manager.owned(id, who.access);
		return res.json(ok({ data: { account: { phone: session?.phone || null }, ...connectionState(session) } }));
	}));

	router.all(["/get_qrcode", "/relogin_qrcode", "/reconnect"], one(async (req, res, who, id) => {
		if (req.path.endsWith("relogin_qrcode")) {
			const current = manager.owned(id, who.access);
			if (current) await current.logout();
		}
		const session = await manager.start(id, who.access, { webhook_style: "waclient" });
		if (session.state !== "connected" && !session.qr) {
			await session.waitFor(() => session.qr || session.state === "connected", 8000);
		}
		return res.json(ok({ base64: session.qr, instance_id: id, data: connectionState(session) }));
	}));

	router.all(["/get_paircode", "/relogin_paircode"], one(async (req, res, who, id) => {
		const session = await manager.open(id, who.access, { webhook_style: "waclient" });
		const paired = await session.requestPairingCode(who.body.phone);
		return res.json(ok({ paircode: paired.pairing_code, instance_id: id }));
	}));

	router.post("/set_webhook", one(async (req, res, who, id) => {
		const url = String(who.body.webhook_url || "").trim();
		if (!url) return fail(res, 400, "webhook_url is required");
		const session = await manager.open(id, who.access, {
			webhook_url: url,
			webhook_style: "waclient",
		});
		return res.json(ok({ data: { webhook_url: session.webhookUrl, enabled: true } }));
	}));

	router.post("/get_webhook", one(async (req, res, who, id) => {
		const session = await live(accessOf(who), id);
		return res.json(ok({
			data: { webhook: { webhook_url: session.webhookUrl || "", enabled: Boolean(session.webhookUrl) } },
		}));
	}));

	router.post("/logout", one(async (req, res, who, id) => {
		const session = manager.owned(id, who.access);
		if (session) await session.logout();
		return res.json(ok({ instance_id: id }));
	}));

	router.post("/delete_instance", one(async (req, res, who, id) => {
		await manager.remove(id, who.access);
		return res.json(ok({ instance_id: id }));
	}));

	router.post("/send", one(async (req, res, who, id) => {
		const session = manager.owned(id, who.access);
		if (!session || session.state !== "connected") {
			return fail(res, 400, "instance is not connected");
		}
		const to = who.body.chat_id || who.body.number;
		const sent = await sendLikeWaClient(session, to, who.body);
		return res.json(ok({
			message_payload: { key: { id: sent.message_id, remoteJid: sent.chat_id, fromMe: true } },
		}));
	}));

	router.post("/check_number", one(async (req, res, who, id) => {
		const session = manager.owned(id, who.access);
		if (!session) return fail(res, 404, "unknown instance");
		const numbers = who.body.numbers || (who.body.number ? [who.body.number] : []);
		const results = [];
		for (const number of numbers) {
			results.push({ number, ...(await session.checkNumber(number)) });
		}
		return res.json(ok({ data: { results } }));
	}));

	return router;
}

function mediaKind(type, url) {
	if (type && type !== "media") return type;
	const name = String(url || "").toLowerCase();
	if (/\.(png|jpe?g|gif|webp)(\?|$)/.test(name)) return "image";
	if (/\.(mp4|mov|webm)(\?|$)/.test(name)) return "video";
	if (/\.(mp3|ogg|m4a|opus)(\?|$)/.test(name)) return "audio";
	return "document";
}

function accessOf(who) {
	return who.access;
}

async function sendLikeWaClient(session, to, body) {
	const type = String(body.type || "text");
	if (type === "media" || type === "image" || type === "document" || type === "video" || type === "audio") {
		return session.sendMedia(to, {
			url: body.media_url || body.url,
			base64: body.base64,
			caption: typeof body.message === "string" ? body.message : body.caption,
			filename: body.filename,
			mimetype: body.mimetype,
			kind: mediaKind(type, body.media_url || body.url),
		});
	}
	if (type === "link") {
		return session.sendText(to, [body.message, body.url].filter(Boolean).join("\n"));
	}
	const text = typeof body.message === "string" ? body.message : body.text || "";
	return session.sendText(to, text);
}
