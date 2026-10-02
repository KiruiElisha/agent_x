/** HTTP surface the Frappe app talks to. */

import crypto from "node:crypto";
import express from "express";

import { requireAdmin, requireToken } from "./auth.js";
import { config } from "./config.js";
import { logger } from "./logger.js";
import { getAdminEmail, setAdminEmail } from "./adminStore.js";
import { mailConfigured, publicOrigin, sendWelcome } from "./mail.js";
import { listMail, publicMail, saveMailSettings } from "./mailserver.js";
import { charge, clearPaid, describeClient, grant, isActive, loadBilling, markPaid, notifySite, publicOffer, requireSending, saveConfig, startTrial } from "./billing.js";
import { createTenant, clientIdFor, deleteTenant, findTenantByEmail, getTenant, listTenants, setTenantEnabled } from "./tenants.js";

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

/** One instance per client. Opening it again does not unlink a phone that is already connected. */
export async function ensureInstance(manager, tenantId) {
	const access = { role: "tenant", id: tenantId };
	const existing = manager.list(access);
	if (existing.length) return existing;
	await manager.open(newInstanceId(), access, { webhook_style: "waclient" });
	return manager.list(access);
}

const codes = new Map();
const SERVICE = "service";

function serviceSession(manager) {
	return manager.owned(SERVICE, { role: "admin" });
}

async function sendFromService(manager, to, text) {
	const session = serviceSession(manager);
	if (!session || session.state !== "connected") return false;
	await session.sendText(to, text);
	return true;
}

async function notifyPaid(manager, email, until) {
	const key = String(email || "").toLowerCase();
	const tenant = (await listTenants()).find((row) => String(row.email || "").toLowerCase() === key);
	const phone = tenant
		? manager.list({ role: "tenant", id: tenant.id }).find((session) => session.phone)?.phone
		: "";
	if (!phone) return;
	const when = until ? String(until).replace("T", " ").slice(0, 16) : "";
	try {
		await sendFromService(
			manager,
			phone,
			`Your WhatsApp API subscription is active${when ? ` until ${when}` : ""}.`,
		);
	} catch (error) {
		logger.warn({ err: error.message, email: key }, "could not send the subscription notice");
	}
}

export function buildRouter(manager) {
	const router = express.Router();

	router.get(
		"/admin",
		wrap(async (_req, res) => res.json({ ok: true, configured: Boolean(getAdminEmail()) })),
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
			const created = await createTenant(clientIdFor(email, (req.body || {}).id), email, { limit: config.maxTenants });
			const trialUntil = await startTrial(email);
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
						trialUntil,
					});
					emailed = true;
				} catch (error) {
					logger.warn({ err: error.message, client: created.id }, "could not email the client token");
				}
			}
			await notifySite(created);
			return res.json({
				ok: true,
				emailed,
				bridge_url: origin,
				instance_id: instanceId,
				...created,
			});
		}),
	);

	router.get(
		"/billing/offer",
		wrap(async (_req, res) => res.json({ ok: true, ...(publicOffer(await loadBilling())) })),
	);

	router.use(requireToken);

	router.post(
		"/billing/config",
		requireAdmin,
		wrap(async (req, res) => res.json({ ok: true, ...(await saveConfig(req.body || {})) })),
	);

	router.post(
		"/billing/grant",
		requireAdmin,
		wrap(async (req, res) => {
			const body = req.body || {};
			const saved = await grant(body.email, body.paid_until, body);
			await notifyPaid(manager, body.email, body.paid_until);
			return res.json({ ok: true, emailed: Boolean(saved?.emailed) });
		}),
	);

	router.get(
		"/billing/status",
		wrap(async (req, res) => {
			const billing = await loadBilling();
			const tenant = req.access?.role === "tenant" ? await getTenant(req.access.id) : null;
			const view = tenant ? describeClient(tenant, billing) : null;
			return res.json({
				ok: true,
				enabled: Boolean(billing.enabled),
				required: Boolean(billing.required),
				amount: billing.amount,
				currency: billing.currency,
				method: billing.method,
				active: Boolean(view?.active),
				payment: view?.payment || "Not paid",
				trial_until: view?.trial_until || null,
				paid_until: view?.paid_until || null,
			});
		}),
	);

	router.post(
		"/billing/pay",
		wrap(async (req, res) => {
			if (req.access?.role !== "tenant") {
				return res.status(403).json({ ok: false, error: "Sign in as a client to pay" });
			}
			const tenant = await getTenant(req.access.id);
			if (!tenant?.email) return res.status(404).json({ ok: false, error: "This client has no email" });
			const billing = await loadBilling();
			if (!billing.enabled || !billing.secret_key) {
				return res.status(400).json({ ok: false, error: "Subscriptions are not configured" });
			}
			const result = await charge(billing, tenant.email, (req.body || {}).phone);
			return res.json({ ok: true, ...result });
		}),
	);

	router.get(
		"/billing/clients",
		requireAdmin,
		wrap(async (_req, res) => {
			const billing = await loadBilling();
			const clients = (await listTenants()).map((tenant) => describeClient(tenant, billing));
			return res.json({
				ok: true,
				clients,
				plan: publicOffer(billing),
				service: { instance_id: "service" },
			});
		}),
	);

	router.post(
		"/billing/clients/:id",
		requireAdmin,
		wrap(async (req, res) => {
			const body = req.body || {};
			if (body.enabled !== undefined) {
				const enabled = body.enabled === true || body.enabled === 1 || body.enabled === "1" || body.enabled === "true"
					? true
					: body.enabled === false || body.enabled === 0 || body.enabled === "0" || body.enabled === "false"
						? false
						: null;
				if (enabled === null) return res.status(400).json({ ok: false, error: "enabled must be true or false" });
				const updated = await setTenantEnabled(req.params.id, enabled);
				if (!updated) return res.status(404).json({ ok: false, error: "unknown client" });
			}
			const tenant = await getTenant(req.params.id);
			if (!tenant) return res.status(404).json({ ok: false, error: "unknown client" });
			if (body.status === "Active") {
				if (!tenant.email) return res.status(400).json({ ok: false, error: "This client has no email" });
				const billing = await loadBilling();
				await markPaid(tenant.email, billing.period_days);
			} else if (body.status === "Trial") {
				if (!tenant.email) return res.status(400).json({ ok: false, error: "This client has no email" });
				await startTrial(tenant.email);
			} else if (body.status) {
				await clearPaid(tenant.email);
			}
			const billing = await loadBilling();
			const client = describeClient(await getTenant(req.params.id), billing);
			if (body.status === "Active") await notifyPaid(manager, client.email, client.paid_until);
			return res.json({ ok: true, client });
		}),
	);

	router.get(
		"/mail",
		requireAdmin,
		wrap(async (_req, res) => res.json({ ok: true, messages: await listMail() })),
	);

	router.get(
		"/mail/config",
		requireAdmin,
		wrap(async (_req, res) => res.json({ ok: true, ...publicMail() })),
	);

	router.post(
		"/mail/config",
		requireAdmin,
		wrap(async (req, res) => res.json({ ok: true, ...(await saveMailSettings(req.body || {})) })),
	);

	router.post(
		"/admin",
		requireAdmin,
		wrap(async (req, res) => {
			const saved = await setAdminEmail((req.body || {}).email);
			return res.json({ ok: true, configured: true, email: saved });
		}),
	);

	router.post(
		"/tenants",
		requireAdmin,
		wrap(async (req, res) => {
			const created = await createTenant((req.body || {}).id);
			const sessions = await ensureInstance(manager, created.id);
			await notifySite(created);
			return res.json({ ok: true, instance_id: sessions[0]?.session || "", ...created });
		}),
	);

	router.get(
		"/tenants",
		requireAdmin,
		wrap(async (_req, res) => {
			const billing = await loadBilling();
			const tenants = (await listTenants()).map((tenant) => describeClient(tenant, billing));
			return res.json({ ok: true, tenants });
		}),
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
		"/sessions",
		wrap(async (req, res) => {
			if (req.access?.role !== "tenant") {
				return res.status(403).json({ ok: false, error: "Sign in as a client to add a number" });
			}
			const id = newInstanceId();
			await manager.open(id, req.access, { webhook_style: "waclient" });
			return res.json({ ok: true, instance_id: id });
		}),
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
			if (req.access?.role === "tenant") {
				await requireSending(await getTenant(req.access.id));
			}

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
