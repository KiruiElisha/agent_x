/** Configuration, read once from the environment. */

import path from "node:path";

function required(name) {
	const value = process.env[name];
	if (!value) {
		console.error(`[config] ${name} is required but not set. Refusing to start.`);
		process.exit(1);
	}
	return value;
}

export const config = {
	port: Number(process.env.BRIDGE_PORT || 8787),
	host: process.env.BRIDGE_HOST || "127.0.0.1",

	// Frappe calls us with this; we reject anything else. No default on purpose:
	// an unauthenticated bridge lets anyone send from the linked number.
	apiToken: required("BRIDGE_API_TOKEN"),

	// Where Baileys credentials live. One subdirectory per session.
	sessionDir: path.resolve(process.env.BRIDGE_SESSION_DIR || "./sessions"),

	// Frappe endpoint we post inbound events to.
	webhookUrl: process.env.BRIDGE_WEBHOOK_URL || "",
	// Shared secret for the HMAC signature on those posts.
	webhookSecret: process.env.BRIDGE_WEBHOOK_SECRET || "",
	webhookTimeoutMs: Number(process.env.BRIDGE_WEBHOOK_TIMEOUT_MS || 15000),
	webhookRetries: Number(process.env.BRIDGE_WEBHOOK_RETRIES || 3),

	logLevel: process.env.BRIDGE_LOG_LEVEL || "info",

	// Baileys can only decrypt media while it still holds the message object,
	// so small audio is downloaded at arrival and inlined in the webhook.
	// Anything larger is skipped rather than buffered.
	inlineAudioMaxBytes: Number(process.env.BRIDGE_INLINE_AUDIO_MAX_MB || 8) * 1024 * 1024,

	// A QR is only valid for about a minute; Baileys rotates it for us.
	// Give up pairing after this many rotations so a forgotten session
	// does not spin forever.
	maxQrRetries: Number(process.env.BRIDGE_MAX_QR_RETRIES || 5),
};
