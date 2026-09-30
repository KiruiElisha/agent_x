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
	// Stay on loopback. A site that already has a domain publishes this port
	// at /agentx-bridge, so a cloud ERPNext never needs this host opened.
	host: process.env.BRIDGE_HOST || "127.0.0.1",

	// Frappe calls us with this; we reject anything else. No default on purpose:
	// an unauthenticated bridge lets anyone send from the linked number.
	apiToken: required("BRIDGE_API_TOKEN"),

	// Where Baileys credentials live. One subdirectory per session.
	sessionDir: path.resolve(process.env.BRIDGE_SESSION_DIR || "./sessions"),

	// Tokens issued to other sites. The master BRIDGE_API_TOKEN is not in here.
	tenantsFile: path.resolve(
		process.env.BRIDGE_TENANTS_FILE ||
			path.join(path.dirname(path.resolve(process.env.BRIDGE_SESSION_DIR || "./sessions")), "tenants.json"),
	),

	// Fallback for a single local site that has not registered its own webhook.
	// Every session can carry its own URL, which is how a second tenant works.
	webhookUrl: process.env.BRIDGE_WEBHOOK_URL || "",
	// Shared secret for the HMAC signature on those posts. Required: Frappe
	// refuses unsigned events, so a bridge without it would drop every message.
	webhookSecret: required("BRIDGE_WEBHOOK_SECRET"),
	webhookTimeoutMs: Number(process.env.BRIDGE_WEBHOOK_TIMEOUT_MS || 15000),
	webhookRetries: Number(process.env.BRIDGE_WEBHOOK_RETRIES || 3),

	logLevel: process.env.BRIDGE_LOG_LEVEL || "info",

	// The public page at the bridge hostname. Clients sign up here; no Frappe
	// site has to be named when the bridge is installed.
	publicUrl: (process.env.BRIDGE_PUBLIC_URL || "").replace(/\/$/, ""),
	signup: process.env.BRIDGE_SIGNUP !== "0",
	maxTenants: Number(process.env.BRIDGE_MAX_TENANTS || 200),

	smtpHost: process.env.BRIDGE_SMTP_HOST || "",
	smtpPort: Number(process.env.BRIDGE_SMTP_PORT || 587),
	smtpUser: process.env.BRIDGE_SMTP_USER || "",
	smtpPassword: process.env.BRIDGE_SMTP_PASSWORD || "",
	smtpFrom: process.env.BRIDGE_SMTP_FROM || process.env.BRIDGE_SMTP_USER || "",

	// Baileys can only decrypt media while it still holds the message object,
	// so small audio is downloaded at arrival and inlined in the webhook.
	// Anything larger is skipped rather than buffered.
	inlineAudioMaxBytes: Number(process.env.BRIDGE_INLINE_AUDIO_MAX_MB || 8) * 1024 * 1024,

	// A QR is only valid for about a minute; Baileys rotates it for us.
	// Give up pairing after this many rotations so a forgotten session
	// does not spin forever.
	maxQrRetries: Number(process.env.BRIDGE_MAX_QR_RETRIES || 5),
};
