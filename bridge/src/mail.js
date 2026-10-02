/** One plain-text message, only when SMTP settings are present. */

import net from "node:net";
import tls from "node:tls";

import { config } from "./config.js";
import { currentMail, sendOut, storeMail } from "./mailserver.js";

export function mailConfigured() {
	const mail = currentMail();
	return Boolean((mail.smtpHost && mail.smtpFrom) || mail.mailDomain);
}

/** Address shown to the client. Prefer BRIDGE_PUBLIC_URL. */
export function publicOrigin(req) {
	if (config.publicUrl) return config.publicUrl;
	const forwarded = (req.get("x-forwarded-proto") || "").split(",")[0].trim();
	const proto = forwarded || req.protocol || "https";
	const host = (req.get("x-forwarded-host") || req.get("host") || "").split(",")[0].trim();
	return host ? `${proto}://${host}` : "";
}

export async function sendWelcome({ to, id, token, origin, instanceId, trialUntil }) {
	const url = origin || config.publicUrl || "";
	const until = trialUntil ? String(trialUntil).replace("T", " ").slice(0, 16) : "";
	const text = [
		"Your WhatsApp bridge client is ready.",
		"",
		`Client: ${id}`,
		instanceId ? `Instance ID: ${instanceId}` : "",
		`Bridge URL: ${url}`,
		`Access token: ${token}`,
		until ? `Trial: sending is included until ${until}.` : "",
		"",
		"In ERPNext or another app, set the API address to the bridge URL,",
		"and paste the instance id and access token into the existing fields.",
		"",
		"Keep the token private. Sign in again with this email to copy it.",
	].filter((line) => line !== undefined).join("\n");

	await deliver({ to, subject: "Your WhatsApp bridge client", text });
}

export async function sendReceipt({ to, amount, currency, paidUntil, invoiceId, plan }) {
	const when = paidUntil ? String(paidUntil).replace("T", " ").slice(0, 16) : "";
	const text = [
		"This is your receipt for WhatsApp API.",
		"",
		`Plan: ${plan || "WhatsApp API"}`,
		`Amount: ${amount ?? ""} ${currency || ""}`.trim(),
		invoiceId ? `Invoice: ${invoiceId}` : null,
		when ? `Paid until: ${when}` : null,
		"",
		"The subscription covers sending from the number linked to this email.",
	].filter((line) => line !== null).join("\n");
	await deliver({ to, subject: "Your WhatsApp API receipt", text });
}

async function deliver({ to, subject, text }) {
	const mail = currentMail();
	const from = mail.smtpFrom || (mail.mailDomain ? `WhatsApp API <receipts@${mail.mailDomain}>` : "");
	if (mail.smtpHost && mail.smtpFrom) {
		try {
			await deliverViaRelay({ to, subject, text, from: mail.smtpFrom, mail });
			await storeMail({
				direction: "out",
				from: mail.smtpFrom,
				to,
				subject,
				text,
				status: "sent",
			});
			return;
		} catch (error) {
			await storeMail({
				direction: "out",
				from: mail.smtpFrom,
				to,
				subject,
				text,
				status: "failed",
				error: error.message,
			});
			throw error;
		}
	}
	await sendOut({ to, subject, text, from });
}

async function deliverViaRelay({ to, subject, text, from, mail }) {
	const body = dotStuff(
		[
			`From: ${mail.smtpFrom}`,
			`To: ${to}`,
			`Subject: ${subject}`,
			"MIME-Version: 1.0",
			"Content-Type: text/plain; charset=utf-8",
			"",
			text,
		].join("\r\n"),
	);

	const port = mail.smtpPort || 587;
	let socket = await open(mail.smtpHost, port, port === 465);
	await expect(socket, 220);
	await command(socket, "EHLO agentx");
	if (port !== 465) {
		await command(socket, "STARTTLS", 220);
		socket = await startTls(socket, mail.smtpHost);
		await command(socket, "EHLO agentx");
	}
	if (mail.smtpUser) {
		await command(socket, "AUTH LOGIN");
		await command(socket, b64(mail.smtpUser));
		await command(socket, b64(mail.smtpPassword || ""));
	}
	await command(socket, `MAIL FROM:<${addressOf(from)}>`);
	await command(socket, `RCPT TO:<${to}>`);
	await command(socket, "DATA", 354);
	await command(socket, body + "\r\n.");
	await command(socket, "QUIT", 221);
	socket.end();
}

function addressOf(from) {
	const match = String(from).match(/<([^>]+)>/);
	return (match ? match[1] : from).trim();
}

function b64(value) {
	return Buffer.from(String(value)).toString("base64");
}

function dotStuff(text) {
	return text
		.split("\r\n")
		.map((line) => (line.startsWith(".") ? "." + line : line))
		.join("\r\n");
}

function open(host, port, secure) {
	return new Promise((resolve, reject) => {
		const socket = secure
			? tls.connect({ host, port, servername: host })
			: net.connect({ host, port });
		remember(socket);
		socket.setTimeout(15000, () => {
			socket.destroy();
			reject(new Error("SMTP timed out"));
		});
		socket.once("error", reject);
		socket.once(secure ? "secureConnect" : "connect", () => resolve(socket));
	});
}

function startTls(socket, host) {
	return new Promise((resolve, reject) => {
		const secureSocket = tls.connect({ socket, servername: host }, () => resolve(secureSocket));
		remember(secureSocket);
		secureSocket.setTimeout(15000, () => {
			secureSocket.destroy();
			reject(new Error("SMTP timed out"));
		});
		secureSocket.once("error", reject);
	});
}

function remember(socket) {
	socket._smtp = "";
	socket.on("data", (chunk) => {
		socket._smtp += chunk.toString("utf8");
	});
}

function command(socket, line, expected) {
	socket.write(line + "\r\n");
	return expect(socket, expected);
}

function expect(socket, code) {
	return new Promise((resolve, reject) => {
		const finish = (status, line) => {
			socket._smtp = "";
			if (code && status !== code) return reject(new Error(line));
			if (status >= 400) return reject(new Error(line));
			resolve(status);
		};
		const check = () => {
			if (!String(socket._smtp || "").endsWith("\n")) return;
			const lines = socket._smtp.split(/\r?\n/).filter(Boolean);
			const last = lines[lines.length - 1] || "";
			if (last.length < 4 || last[3] === "-") return;
			socket.off("data", check);
			finish(Number(last.slice(0, 3)), last);
		};
		socket.on("data", check);
		check();
	});
}
