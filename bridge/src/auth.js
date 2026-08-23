/** Bearer token check for every route. */

import crypto from "node:crypto";

import { config } from "./config.js";

function safeEqual(a, b) {
	const left = Buffer.from(String(a));
	const right = Buffer.from(String(b));
	// timingSafeEqual throws on a length mismatch, so compare lengths first.
	if (left.length !== right.length) return false;
	return crypto.timingSafeEqual(left, right);
}

export function requireToken(req, res, next) {
	const header = req.get("Authorization") || "";
	const token = header.startsWith("Bearer ") ? header.slice(7) : req.get("X-Api-Token") || "";

	if (!token || !safeEqual(token, config.apiToken)) {
		return res.status(401).json({ ok: false, error: "unauthorised" });
	}

	return next();
}
