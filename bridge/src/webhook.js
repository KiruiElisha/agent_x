/** Posts events to Frappe, signed so the receiver can trust them. */

import crypto from "node:crypto";

import { config } from "./config.js";
import { logger } from "./logger.js";

function sign(body) {
	if (!config.webhookSecret) return null;
	return crypto.createHmac("sha256", config.webhookSecret).update(body).digest("hex");
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Deliver one event. Retries on network errors and 5xx, but never on 4xx:
 * a rejected payload will be rejected again, and retrying just amplifies it.
 */
export async function deliver(event) {
	if (!config.webhookUrl) {
		logger.debug({ event: event.event }, "no webhook url configured, dropping event");
		return false;
	}

	const body = JSON.stringify(event);
	const headers = { "Content-Type": "application/json" };

	const signature = sign(body);
	if (signature) headers["X-AgentX-Signature"] = `sha256=${signature}`;

	for (let attempt = 1; attempt <= config.webhookRetries; attempt++) {
		try {
			const response = await fetch(config.webhookUrl, {
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
