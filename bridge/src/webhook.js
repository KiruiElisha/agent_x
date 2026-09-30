/** Posts events to Frappe, signed so the receiver can trust them. */

import crypto from "node:crypto";

import { config } from "./config.js";
import { logger } from "./logger.js";

function sign(body, secret) {
	if (!secret) return null;
	return crypto.createHmac("sha256", secret).update(body).digest("hex");
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Deliver one event. Retries on network errors and 5xx, but never on 4xx:
 * a rejected payload will be rejected again, and retrying just amplifies it.
 *
 * `target` is the session's own webhook. A session with none falls back to the
 * single URL in the environment, which is the one-site setup.
 */
export async function deliver(event, target = {}) {
	const url = target.webhookUrl || config.webhookUrl;
	const secret = target.unsigned ? "" : target.webhookSecret || config.webhookSecret;

	if (!url) {
		logger.debug({ event: event.event, session: event.session }, "no webhook url, dropping event");
		return false;
	}

	const body = JSON.stringify(event);
	const headers = { "Content-Type": "application/json" };

	const signature = sign(body, secret);
	if (signature) headers["X-AgentX-Signature"] = `sha256=${signature}`;

	for (let attempt = 1; attempt <= config.webhookRetries; attempt++) {
		try {
			const response = await fetch(url, {
				method: "POST",
				headers,
				body,
				signal: AbortSignal.timeout(config.webhookTimeoutMs),
			});

			if (response.ok) return true;

			if (response.status < 500) {
				logger.warn(
					{ status: response.status, event: event.event },
					"webhook rejected the event, not retrying",
				);
				return false;
			}

			logger.warn(
				{ status: response.status, attempt, event: event.event },
				"webhook returned a server error",
			);
		} catch (error) {
			logger.warn({ err: error.message, attempt, event: event.event }, "webhook delivery failed");
		}

		if (attempt < config.webhookRetries) await sleep(1000 * 2 ** (attempt - 1));
	}

	logger.error({ event: event.event }, "giving up on webhook delivery");
	return false;
}
