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
			body: JSON.stringify({
				email,
				client_id: client.id || "",
				payment: client.payment || "",
				paid_until: client.paid_until || null,
				trial_until: client.trial_until || null,
			}),
		});
	} catch (error) {
		const { logger } = await import("./logger.js");
		logger.warn({ err: error.message }, "could not sync the client to ERPNext");
	}
}

const TRIAL_DAYS = 5;

function untilStamp(days) {
	const span = Math.max(Number(days) || 1, 1);
	return new Date(Date.now() + span * 24 * 60 * 60 * 1000).toISOString();
}

function future(value) {
	if (!value) return false;
	const time = new Date(value).getTime();
	return !Number.isNaN(time) && time > Date.now();
}

export function isActive(billing, email) {
	const key = String(email || "").toLowerCase();
	return future(billing.paid?.[key]) || future(billing.trials?.[key]);
}

export async function startTrial(email) {
	const key = String(email || "").toLowerCase();
	if (!key) return null;
	const billing = await loadBilling();
	billing.trials = billing.trials || {};
	billing.trials[key] = untilStamp(TRIAL_DAYS);
	await save(billing);
	return billing.trials[key];
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

export async function clearPaid(email) {
	const billing = await loadBilling();
	const key = String(email || "").toLowerCase();
	if (billing.paid && key) delete billing.paid[key];
	if (billing.trials && key) delete billing.trials[key];
	await save(billing);
}

export async function markPaid(email, days) {
	const until = untilStamp(days || 30);
	return grant(email, until, {});
}

export function describeClient(tenant, billing) {
	const email = String(tenant.email || "").toLowerCase();
	const paidUntil = email ? billing.paid?.[email] || null : null;
	const trialUntil = email ? billing.trials?.[email] || null : null;
	const paid = future(paidUntil);
	const trial = future(trialUntil);
	let payment = "Not paid";
	if (!email) payment = "No email";
	else if (paid) payment = "Active";
	else if (trial) payment = "Trial";
	return {
		id: tenant.id,
		email: tenant.email || "",
		created_at: tenant.created_at || null,
		disabled: Boolean(tenant.disabled),
		paid_until: paid ? paidUntil : null,
		trial_until: trialUntil,
		active: paid || trial,
		payment,
	};
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

export async function requireSending(tenant) {
	if (tenant?.disabled) {
		const error = new Error("This client is disabled");
		error.statusCode = 403;
		throw error;
	}
	await requirePaid(tenant?.email);
}
