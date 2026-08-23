/** HTTP surface the Frappe app talks to. */

import express from "express";

import { requireToken } from "./auth.js";
import { logger } from "./logger.js";

/** Turns a rejected promise into a JSON error instead of an unhandled rejection. */
function wrap(handler) {
	return (req, res, next) => Promise.resolve(handler(req, res, next)).catch(next);
}

export function buildRouter(manager) {
	const router = express.Router();

	router.use(requireToken);

	router.get(
		"/sessions",
		wrap(async (req, res) => res.json({ ok: true, sessions: manager.list() })),
	);

	router.post(
		"/sessions/:id/start",
		wrap(async (req, res) => res.json({ ok: true, ...(await manager.start(req.params.id)) })),
	);

	router.get(
		"/sessions/:id/status",
		wrap(async (req, res) => {
			const session = manager.get(req.params.id);
			if (!session) return res.status(404).json({ ok: false, error: "unknown session" });
			return res.json({ ok: true, ...session.status });
		}),
	);

	router.get(
		"/sessions/:id/qr",
		wrap(async (req, res) => {
			const session = manager.get(req.params.id);
			if (!session) return res.status(404).json({ ok: false, error: "unknown session" });

			return res.json({
				ok: true,
				session: session.id,
				state: session.state,
				qr: session.qr,
				expires_at: session.qrExpiresAt,
			});
		}),
	);

	router.post(
		"/sessions/:id/stop",
		wrap(async (req, res) => {
			const session = manager.get(req.params.id);
			if (!session) return res.status(404).json({ ok: false, error: "unknown session" });
			return res.json({ ok: true, ...(await session.stop()) });
		}),
	);

	router.post(
		"/sessions/:id/logout",
		wrap(async (req, res) => {
			const session = manager.get(req.params.id);
			if (!session) return res.status(404).json({ ok: false, error: "unknown session" });
			return res.json({ ok: true, ...(await session.logout()) });
		}),
	);

	router.delete(
		"/sessions/:id",
		wrap(async (req, res) => res.json({ ok: true, ...(await manager.remove(req.params.id)) })),
	);

	router.post(
		"/sessions/:id/send",
		wrap(async (req, res) => {
			const session = manager.get(req.params.id);
			if (!session) return res.status(404).json({ ok: false, error: "unknown session" });

			const { to, text, media } = req.body || {};
			if (!to) return res.status(400).json({ ok: false, error: "to is required" });

			const result = media
				? await session.sendMedia(to, media)
				: await session.sendText(to, text);

			return res.json({ ok: true, ...result });
		}),
	);

	router.post(
		"/sessions/:id/check",
		wrap(async (req, res) => {
			const session = manager.get(req.params.id);
			if (!session) return res.status(404).json({ ok: false, error: "unknown session" });

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
	// Baileys errors are operational, not bugs, so 400 rather than 500.
	const status = error.output?.statusCode || 400;
	res.status(status).json({ ok: false, error: error.message });
}
