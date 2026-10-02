/** Subscriptions. The operator's ERPNext site pushes the plan here. */

import fs from "node:fs/promises";
import path from "node:path";

import { config } from "./config.js";

const EMPTY = {
	enabled: false,
	required: false,
	sandbox: true,
	secret_key: "",
	challenge: "",
	amount: 0,
	currency: "KES",
	method: "M-Pesa prompt",
	plan_name: "WhatsApp API",
	frequency: 1,
	frequency_unit: "M",
	billing_cycles: 0,
	period_days: 30,
	paid: {},
	site_url: "",
};

function file() {
	return path.join(path.dirname(config.tenantsFile), "billing.json");
}

export async function loadBilling() {
	try {
		return { ...EMPTY, ...JSON.parse(await fs.readFile(file(), "utf8")) };
	} catch (error) {
		if (error.code === "ENOENT") return { ...EMPTY };
		throw error;
	}
}

async function save(billing) {
	await fs.mkdir(path.dirname(file()), { recursive: true });
	const temporary = `${file()}.tmp`;
	await fs.writeFile(temporary, JSON.stringify(billing));
	await fs.rename(temporary, file());
}

export async function saveConfig(incoming) {
	const current = await loadBilling();
	const next = { ...current, ...incoming, paid: current.paid || {} };
	await save(next);
	return { enabled: next.enabled, amount: next.amount, currency: next.currency };
}

export function publicOffer(billing) {
	return {
		enabled: Boolean(billing.enabled),
		amount: billing.amount,
		currency: billing.currency,
		method: billing.method,
		period_days: billing.period_days,
		plan_name: billing.plan_name || "WhatsApp API",
	};
}

export async function grant(email, paidUntil, receipt = {}) {
	const billing = await loadBilling();
	const key = String(email || "").toLowerCase();
	billing.paid = billing.paid || {};
	billing.paid[key] = paidUntil;
	await save(billing);
	let emailed = false;
	if (key) {
		try {
			const { sendReceipt } = await import("./mail.js");
			await sendReceipt({
				to: key,
				amount: receipt.amount ?? billing.amount,
				currency: receipt.currency || billing.currency,
				paidUntil,
				invoiceId: receipt.invoice_id || "",
				plan: receipt.plan_name || billing.plan_name,
			});
			emailed = true;
		} catch (error) {
			const { logger } = await import("./logger.js");
			logger.warn({ err: error.message, email: key }, "receipt was not emailed");
		}
	}
	return { emailed };
}

export async function notifySite(client) {
	const billing = await loadBilling();
	const url = String(billing.site_url || "");
	const email = String(client?.email || "").trim().toLowerCase();
	if (!url || !email) return;
	try {
		await fetch(url, {
			method: "POST",
			headers: {
				Authorization: `Bearer ${config.apiToken}`,
				Accept: "application/json",
				"Content-Type": "application/json",
			},
			body: JSON.stringify({ email, client_id: client.id || "" }),
		});
	} catch (error) {
		const { logger } = await import("./logger.js");
		logger.warn({ err: error.message }, "could not sync the client to ERPNext");
	}
}

export function isActive(billing, email) {
	const until = billing.paid?.[String(email || "").toLowerCase()];
	if (!until) return false;
	return new Date(until).getTime() > Date.now();
}

export async function charge(billing, email, phone) {
	const base = billing.sandbox ? "https://sandbox.intasend.com/api/v1/" : "https://api.intasend.com/api/v1/";
	const headers = {
		Authorization: `Bearer ${billing.secret_key}`,
		Accept: "application/json",
		"Content-Type": "application/json",
	};
	const ref = `sub:${email}`;

	async function post(path, payload) {
		const response = await fetch(base + path, { method: "POST", headers, body: JSON.stringify(payload) });
		const body = await response.json().catch(() => ({}));
		if (!response.ok) {
			const error = new Error(body.detail || body.message || "IntaSend refused the payment");
			error.statusCode = 400;
			throw error;
		}
		return body;
	}

	if (billing.method === "Card subscription") {
		const customer = await post("subscriptions-customers/", {
			email,
			first_name: (email.split("@")[0] || "Client").slice(0, 40),
			last_name: "Client",
			country: "KE",
			reference: ref,
		});
		const plan = await post("subscriptions-plans/", {
			name: billing.plan_name || "WhatsApp API",
			amount: billing.amount,
			currency: billing.currency || "KES",
			frequency: billing.frequency || 1,
			frequency_unit: billing.frequency_unit || "M",
			billing_cycles: billing.billing_cycles || 0,
		});
		const created = await post("subscriptions/", {
			customer_id: customer.customer_id,
			plan_id: plan.plan_id,
			reference: ref,
		});
		return { setup_url: created.setup_url || "", subscription_id: created.id || created.subscription_id || "" };
	}

	const digits = String(phone || "").replace(/\D/g, "");
	if (!digits) {
		const error = new Error("An M-Pesa phone number is required");
		error.statusCode = 400;
		throw error;
	}
	const body = await post("payment/mpesa-stk-push/", {
		amount: billing.amount,
		phone_number: digits,
		api_ref: ref,
	});
	return { invoice_id: body.invoice?.invoice_id || "", state: body.invoice?.state || "PENDING" };
}

export async function requirePaid(email) {
	const billing = await loadBilling();
	if (!billing.enabled || !billing.required) return;
	if (!isActive(billing, email)) {
		const error = new Error("An active subscription is required");
		error.statusCode = 402;
		throw error;
	}
}
