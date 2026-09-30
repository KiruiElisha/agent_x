/** HTTP surface the Frappe app talks to. */

import crypto from "node:crypto";
import express from "express";

import { requireAdmin, requireToken } from "./auth.js";
import { config } from "./config.js";
import { logger } from "./logger.js";
import { getAdminEmail, setAdminEmail } from "./adminStore.js";
import { mailConfigured, publicOrigin, sendWelcome } from "./mail.js";
import { createTenant, deleteTenant, findTenantByEmail, listTenants } from "./tenants.js";

/** Turns a rejected promise into a JSON error instead of an unhandled rejection. */
function wrap(handler) {
	return (req, res, next) => Promise.resolve(handler(req, res, next)).catch(next);
}

function missing(res, id) {
	return res.status(404).json({ ok: false, error: "unknown session" });
}

function newInstanceId() {
	return crypto.randomBytes(7).toString("hex").toUpperCase();
}

const codes = new Map();
const SERVICE = "service";

async function ensureInstance(manager, tenantId) {
	const access = { role: "tenant", id: tenantId };
	const existing = manager.list(access);
	if (existing.length) return existing;
	await manager.open(newInstanceId(), access, { webhook_style: "waclient" });
	return manager.list(access);
}

function serviceSession(manager) {
	return manager.owned(SERVICE, { role: "admin" });
}

async function sendFromService(manager, to, text) {
	const session = serviceSession(manager);
	if (!session || session.state !== "connected") return false;
	await session.sendText(to, text);
	return true;
}

export function buildRouter(manager) {
	const router = express.Router();

	router.get(
		"/admin",
		wrap(async (_req, res) => res.json({ ok: true, configured: Boolean(getAdminEmail()) })),
	);

	router.post(
		"/admin",
		wrap(async (req, res) => {
			const saved = await setAdminEmail((req.body || {}).email);
			return res.json({ ok: true, configured: true, email: saved });
		}),
	);

	router.post(
		"/login",
		wrap(async (req, res) => {
			const email = String((req.body || {}).email || "").trim().toLowerCase();
			if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
				return res.status(400).json({ ok: false, error: "A valid email is required" });
			}
			if (getAdminEmail() && email === getAdminEmail()) {
				return res.json({ ok: true, role: "admin", email, token: config.apiToken, instances: [] });
			}
			const tenant = await findTenantByEmail(email);
			if (!tenant) return res.status(404).json({ ok: false, error: "No client uses that email" });
			const sessions = await ensureInstance(manager, tenant.id);
			const phone = (sessions.find((session) => session.phone) || {}).phone;
			if (phone && serviceSession(manager)?.state === "connected") {
				const code = String(crypto.randomInt(100000, 1000000));
				codes.set(email, { code, expires: Date.now() + 10 * 60 * 1000 });
				try {
					await sendFromService(
						manager,
						phone,
						`Your WhatsApp API sign-in code is ${code}. It expires in 10 minutes.`,
					);
					return res.json({ ok: true, role: "client", email, verification_required: true });
				} catch (error) {
					codes.delete(email);
					logger.warn({ err: error.message }, "could not send a verification code");
				}
			}
			return res.json({
				ok: true,
				role: "client",
				id: tenant.id,
				email,
				token: tenant.token,
				instances: sessions.map((session) => ({
					instance_id: session.session,
					state: session.state,
					phone: session.phone,
				})),
			});
		}),
	);

	router.post(
		"/login/code",
		wrap(async (req, res) => {
			const email = String((req.body || {}).email || "").trim().toLowerCase();
			const code = String((req.body || {}).code || "").trim();
			const pending = codes.get(email);
			if (!pending || pending.expires < Date.now() || pending.code !== code) {
				return res.status(401).json({ ok: false, error: "That code is not valid" });
			}
			codes.delete(email);
			const tenant = await findTenantByEmail(email);
			if (!tenant) return res.status(404).json({ ok: false, error: "No client uses that email" });
			const sessions = await ensureInstance(manager, tenant.id);
			return res.json({
				ok: true,
				role: "client",
				id: tenant.id,
				email,
				token: tenant.token,
				instances: sessions.map((session) => ({
					instance_id: session.session,
					state: session.state,
					phone: session.phone,
				})),
			});
		}),
	);

	// Anyone who can open the bridge hostname can create their own client.
	// The token comes back once. It cannot see anyone else's numbers.
	router.post(
		"/signup",
		wrap(async (req, res) => {
			if (!config.signup) {
				return res.status(403).json({ ok: false, error: "signup is disabled" });
			}
			const email = String((req.body || {}).email || "").trim().toLowerCase();
			if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
				return res.status(400).json({ ok: false, error: "A valid email is required" });
			}
			const created = await createTenant((req.body || {}).id, email, { limit: config.maxTenants });
			const sessions = await ensureInstance(manager, created.id);
			const instanceId = sessions[0]?.session || "";
			const origin = publicOrigin(req);
			let emailed = false;
			if (mailConfigured()) {
				try {
					await sendWelcome({
						to: email,
						id: created.id,
						token: created.token,
						origin,
						instanceId,
					});
					emailed = true;
				} catch (error) {
					logger.warn({ err: error.message, client: created.id }, "could not email the client token");
				}
			}
			return res.json({
				ok: true,
				emailed,
				bridge_url: origin,
				instance_id: instanceId,
				...created,
			});
		}),
	);

	router.use(requireToken);

	router.post(
		"/tenants",
		requireAdmin,
		wrap(async (req, res) => {
			const created = await createTenant((req.body || {}).id);
			// The token is returned once. It is not listed afterwards.
			return res.json({ ok: true, ...created });
		}),
	);

	router.get(
		"/tenants",
		requireAdmin,
		wrap(async (req, res) => res.json({ ok: true, tenants: await listTenants() })),
	);

	router.delete(
		"/tenants/:id",
		requireAdmin,
		wrap(async (req, res) => {
			const removed = await deleteTenant(req.params.id);
			if (!removed) return res.status(404).json({ ok: false, error: "unknown tenant" });
			return res.json({ ok: true, id: req.params.id, state: "removed" });
		}),
	);

	router.get(
		"/sessions",
		wrap(async (req, res) => res.json({ ok: true, sessions: manager.list(req.access) })),
	);

	router.post(
		"/sessions/:id/start",
		wrap(async (req, res) => {
			const session = await manager.start(req.params.id, req.access, req.body || {});
			// The QR is created a moment after the socket opens. Wait for it so a
			// cloud site, which may never see the webhook, still gets a code.
			if (session.state !== "connected" && !session.qr) {
				await session.waitFor(() => session.qr || session.state === "connected", 8000);
			}
			return res.json({
				ok: true,
				...session.status,
				qr: session.qr,
				expires_at: session.qrExpiresAt,
			});
		}),
	);

	router.post(
		"/sessions/:id/webhook",
		wrap(async (req, res) => {
			const session = await manager.open(req.params.id, req.access, req.body || {});
			return res.json({
				ok: true,
				session: session.publicId,
				webhook_url: session.webhookUrl,
			});
		}),
	);

	router.post(
		"/sessions/:id/pair",
		wrap(async (req, res) => {
			const session = await manager.open(req.params.id, req.access, req.body || {});
			const paired = await session.requestPairingCode((req.body || {}).phone);
			return res.json({ ok: true, session: session.publicId, ...paired });
		}),
	);

	router.get(
		"/sessions/:id/status",
		wrap(async (req, res) => {
			const session = manager.owned(req.params.id, req.access);
			if (!session) return missing(res, req.params.id);
			return res.json({ ok: true, ...session.status });
		}),
	);

	router.get(
		"/sessions/:id/qr",
		wrap(async (req, res) => {
			const session = manager.owned(req.params.id, req.access);
			if (!session) return missing(res, req.params.id);

			return res.json({
				ok: true,
				session: session.publicId,
				state: session.state,
				qr: session.qr,
				expires_at: session.qrExpiresAt,
			});
		}),
	);

	router.post(
		"/sessions/:id/stop",
		wrap(async (req, res) => {
			const session = manager.owned(req.params.id, req.access);
			if (!session) return missing(res, req.params.id);
			return res.json({ ok: true, ...(await session.stop()) });
		}),
	);

	router.post(
		"/sessions/:id/logout",
		wrap(async (req, res) => {
			const session = manager.owned(req.params.id, req.access);
			if (!session) return missing(res, req.params.id);
			return res.json({ ok: true, ...(await session.logout()) });
		}),
	);

	router.delete(
		"/sessions/:id",
		wrap(async (req, res) => res.json({ ok: true, ...(await manager.remove(req.params.id, req.access)) })),
	);

	router.post(
		"/sessions/:id/send",
		wrap(async (req, res) => {
			const session = manager.owned(req.params.id, req.access);
			if (!session) return missing(res, req.params.id);

			const { to, text, media } = req.body || {};
			if (!to) return res.status(400).json({ ok: false, error: "to is required" });

			const result = media ? await session.sendMedia(to, media) : await session.sendText(to, text);

			return res.json({ ok: true, ...result });
		}),
	);

	router.get(
		"/sessions/:id/catalog",
		wrap(async (req, res) => {
			const session = manager.get(req.params.id);
			if (!session) return res.status(404).json({ ok: false, error: "unknown session" });

			const { jid, limit, cursor } = req.query;
			const products = await session.getCatalog(jid, {
				limit: limit ? Number(limit) : undefined,
				cursor,
			});
			return res.json({ ok: true, products });
		}),
	);

	router.get(
		"/sessions/:id/collections",
		wrap(async (req, res) => {
			const session = manager.get(req.params.id);
			if (!session) return res.status(404).json({ ok: false, error: "unknown session" });

			const { jid, limit } = req.query;
			const collections = await session.getCollections(jid, limit ? Number(limit) : undefined);
			return res.json({ ok: true, collections });
		}),
	);

	router.post(
		"/sessions/:id/products",
		wrap(async (req, res) => {
			const session = manager.get(req.params.id);
			if (!session) return res.status(404).json({ ok: false, error: "unknown session" });

			const product = await session.createProduct(req.body || {});
			return res.json({ ok: true, product });
		}),
	);

	router.patch(
		"/sessions/:id/products/:productId",
		wrap(async (req, res) => {
			const session = manager.get(req.params.id);
			if (!session) return res.status(404).json({ ok: false, error: "unknown session" });

			const product = await session.updateProduct(req.params.productId, req.body || {});
			return res.json({ ok: true, product });
		}),
	);

	router.delete(
		"/sessions/:id/products",
		wrap(async (req, res) => {
			const session = manager.get(req.params.id);
			if (!session) return res.status(404).json({ ok: false, error: "unknown session" });

			const { productIds } = req.body || {};
			if (!Array.isArray(productIds) || !productIds.length) {
				return res.status(400).json({ ok: false, error: "productIds must be a non-empty array" });
			}

			return res.json({ ok: true, ...(await session.deleteProducts(productIds)) });
		}),
	);

	router.get(
		"/sessions/:id/orders/:orderId",
		wrap(async (req, res) => {
			const session = manager.get(req.params.id);
			if (!session) return res.status(404).json({ ok: false, error: "unknown session" });

			const { token } = req.query;
			if (!token) return res.status(400).json({ ok: false, error: "token is required" });

			const order = await session.getOrderDetails(req.params.orderId, token);
			return res.json({ ok: true, order });
		}),
	);

	router.post(
		"/sessions/:id/check",
		wrap(async (req, res) => {
			const session = manager.owned(req.params.id, req.access);
			if (!session) return missing(res, req.params.id);

			const { number } = req.body || {};
			if (!number) return res.status(400).json({ ok: false, error: "number is required" });

			return res.json({ ok: true, ...(await session.checkNumber(number)) });
		}),
	);

	return router;
}

/** Last stop for anything thrown in a route. */
export function errorHandler(error, req, res, _next) {
	logger.error({ err: error.message, path: req.path }, "request failed");
	const status = error.statusCode || error.output?.statusCode || 400;
	res.status(status).json({
		ok: false,
		status: "error",
		message: error.message,
		error: error.message,
	});
}
