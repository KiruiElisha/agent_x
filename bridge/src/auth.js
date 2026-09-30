/** Bearer token check for every route. */

import { resolveAccess } from "./tenants.js";

export function requireToken(req, res, next) {
	const header = req.get("Authorization") || "";
	const token = header.startsWith("Bearer ") ? header.slice(7) : req.get("X-Api-Token") || "";

	resolveAccess(token)
		.then((access) => {
			if (!access) return res.status(401).json({ ok: false, error: "unauthorised" });
			req.access = access;
			return next();
		})
		.catch(next);
}

export function requireAdmin(req, res, next) {
	if (req.access?.role !== "admin") {
		return res.status(403).json({ ok: false, error: "the master token is required" });
	}
	return next();
}
