/** Small SMTP listener and outbound delivery.

Inbound mail is accepted only for BRIDGE_MAIL_DOMAIN. Local submissions may
send further. Other recipients are refused, so this is not an open relay.
*/

import dns from "node:dns/promises";
import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";

import { config } from "./config.js";
import { logger } from "./logger.js";

const MAX_BYTES = 256 * 1024;
const KEEP = 400;

function mailbox() {
	return path.join(path.dirname(config.tenantsFile), "mail.json");
}

function settingsFile() {
	return path.join(path.dirname(config.tenantsFile), "mail-settings.json");
}

let storedMail = null;

export async function loadMailSettings() {
	try {
		storedMail = JSON.parse(await fs.readFile(settingsFile(), "utf8"));
	} catch (error) {
		if (error.code !== "ENOENT") throw error;
		storedMail = null;
	}
}

export function currentMail() {
	const saved = storedMail || {};
	return {
		smtpHost: saved.smtpHost || config.smtpHost,
		smtpPort: Number(saved.smtpPort || config.smtpPort || 587),
		smtpUser: saved.smtpUser || config.smtpUser,
		smtpPassword: saved.smtpPassword || config.smtpPassword,
		smtpFrom: saved.smtpFrom || config.smtpFrom,
		mailDomain: String(saved.mailDomain || config.mailDomain || "").toLowerCase(),
	};
}

export function publicMail() {
	const mail = currentMail();
	return {
		smtp_host: mail.smtpHost,
		smtp_port: mail.smtpPort,
		smtp_user: mail.smtpUser,
		smtp_from: mail.smtpFrom,
		mail_domain: mail.mailDomain,
		has_password: Boolean(mail.smtpPassword),
	};
}

export async function saveMailSettings(incoming) {
	const next = { ...(storedMail || {}) };
	const map = {
		smtp_host: "smtpHost",
		smtp_port: "smtpPort",
		smtp_user: "smtpUser",
		smtp_from: "smtpFrom",
		mail_domain: "mailDomain",
	};
	for (const [from, to] of Object.entries(map)) {
		if (incoming[from] !== undefined && incoming[from] !== null) next[to] = incoming[from];
	}
	if (incoming.smtp_password) next.smtpPassword = incoming.smtp_password;
	storedMail = next;
	await fs.mkdir(path.dirname(settingsFile()), { recursive: true });
	const temporary = `${settingsFile()}.tmp`;
	await fs.writeFile(temporary, JSON.stringify(next));
	await fs.rename(temporary, settingsFile());
	return publicMail();
}

function address(value) {
	const match = String(value || "").match(/<([^>]+)>/);
	return (match ? match[1] : String(value || "")).trim().toLowerCase();
}

function domain(email) {
	return address(email).split("@")[1] || "";
}

function loopback(ip) {
	const host = String(ip || "").replace(/^::ffff:/, "");
	return host === "127.0.0.1" || host === "::1";
}

function fromAddress() {
	const mail = currentMail();
	if (mail.smtpFrom) return mail.smtpFrom;
	if (mail.mailDomain) return `WhatsApp API <receipts@${mail.mailDomain}>`;
	return "receipts@localhost";
}

async function readBox() {
	try {
		const rows = JSON.parse(await fs.readFile(mailbox(), "utf8"));
		return Array.isArray(rows) ? rows : [];
	} catch (error) {
		if (error.code === "ENOENT") return [];
		throw error;
	}
}

async function writeBox(rows) {
	await fs.mkdir(path.dirname(mailbox()), { recursive: true });
	const kept = rows.slice(-KEEP);
	const temporary = `${mailbox()}.tmp`;
	await fs.writeFile(temporary, JSON.stringify(kept));
	await fs.rename(temporary, mailbox());
}

export async function listMail() {
	const rows = await readBox();
	return rows.slice().reverse();
}

export async function storeMail(message) {
	const rows = await readBox();
	rows.push({
		id: `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`,
		at: new Date().toISOString(),
		...message,
	});
	await writeBox(rows);
}

function unstuff(raw) {
	return String(raw)
		.replace(/\r\n/g, "\n")
		.split("\n")
		.map((line) => (line.startsWith("..") ? line.slice(1) : line))
		.join("\n");
}

function parsed(raw) {
	const text = unstuff(raw);
	const split = text.indexOf("\n\n");
	const head = split === -1 ? text : text.slice(0, split);
	const body = split === -1 ? "" : text.slice(split + 2);
	const headers = {};
	for (const line of head.split("\n")) {
		const cut = line.indexOf(":");
		if (cut < 1) continue;
		headers[line.slice(0, cut).trim().toLowerCase()] = line.slice(cut + 1).trim();
	}
	return { headers, body: body.trim() };
}

async function accept(envelope, raw) {
	const message = parsed(raw);
	const to = envelope.rcpt;
	if (currentMail().mailDomain && domain(to) === currentMail().mailDomain) {
		await storeMail({
			direction: "in",
			from: envelope.from,
			to,
			subject: message.headers.subject || "",
			text: message.body,
			status: "stored",
		});
		return;
	}
	await sendOut({
		to,
		subject: message.headers.subject || "",
		text: message.body,
		from: envelope.from || fromAddress(),
	});
}

export async function sendOut({ to, subject, text, from }) {
	const sender = from || fromAddress();
	const record = {
		direction: "out",
		from: sender,
		to,
		subject: subject || "",
		text: text || "",
		status: "queued",
	};
	try {
		if (!currentMail().mailDomain) {
			throw new Error("Set BRIDGE_MAIL_DOMAIN or BRIDGE_SMTP_HOST so mail can leave this bridge");
		}
		await direct(sender, to, subject, text);
		record.status = "sent";
	} catch (error) {
		record.status = "failed";
		record.error = error.message;
		logger.warn({ err: error.message, to }, "mail was not delivered");
	}
	await storeMail(record);
	if (record.status === "failed") {
		throw new Error(record.error);
	}
}

function mime({ from, to, subject, text }) {
	return [
		`From: ${from}`,
		`To: ${to}`,
		`Subject: ${subject}`,
		"MIME-Version: 1.0",
		"Content-Type: text/plain; charset=utf-8",
		"",
		text || "",
	].join("\r\n");
}

function dotStuff(text) {
	return text
		.split("\r\n")
		.map((line) => (line.startsWith(".") ? `.${line}` : line))
		.join("\r\n");
}

async function direct(from, to, subject, text) {
	const hostDomain = domain(to);
	if (!hostDomain) throw new Error("That recipient has no domain");
	let hosts = [];
	try {
		const records = await dns.resolveMx(hostDomain);
		records.sort((left, right) => left.priority - right.priority);
		hosts = records.map((row) => row.exchange);
	} catch {
		hosts = [hostDomain];
	}
	let last = new Error("no mail server answered");
	for (const host of hosts) {
		try {
			await smtpSend({
				host,
				port: 25,
				secure: false,
				from: address(from),
				to: address(to),
				body: dotStuff(mime({ from, to, subject, text })),
				ehlo: currentMail().mailDomain,
			});
			return;
		} catch (error) {
			last = error;
		}
	}
	throw last;
}

function smtpSend({ host, port, secure, from, to, body, auth, ehlo }) {
	return new Promise((resolve, reject) => {
		const socket = net.connect({ host, port });
		let buffer = "";
		let stage = "greeting";
		const hello = ehlo || "bridge";
		const fail = (error) => {
			socket.destroy();
			reject(error instanceof Error ? error : new Error(String(error)));
		};
		socket.setTimeout(20000, () => fail(new Error("SMTP timed out")));
		socket.once("error", fail);

		function write(line) {
			socket.write(`${line}\r\n`);
		}

		function take(line) {
			const code = Number(line.slice(0, 3));
			if (line[3] === "-") return;
			if (code >= 400 && code !== 354) return fail(new Error(line));
			if (stage === "greeting") {
				stage = "ehlo";
				write(`EHLO ${hello}`);
			} else if (stage === "ehlo") {
				if (auth) {
					stage = "auth";
					write("AUTH LOGIN");
				} else {
					stage = "from";
					write(`MAIL FROM:<${from}>`);
				}
			} else if (stage === "auth") {
				stage = "user";
				write(Buffer.from(auth.user).toString("base64"));
			} else if (stage === "user") {
				stage = "pass";
				write(Buffer.from(auth.password).toString("base64"));
			} else if (stage === "pass") {
				stage = "from";
				write(`MAIL FROM:<${from}>`);
			} else if (stage === "from") {
				stage = "rcpt";
				write(`RCPT TO:<${to}>`);
			} else if (stage === "rcpt") {
				stage = "data";
				write("DATA");
			} else if (stage === "data") {
				if (code !== 354 && code !== 250) return fail(new Error(line));
				stage = "body";
				socket.write(`${body}\r\n.\r\n`);
			} else if (stage === "body") {
				stage = "quit";
				write("QUIT");
				socket.end();
				resolve();
			}
		}

		socket.on("data", (chunk) => {
			buffer += chunk.toString("utf8");
			let cut;
			while ((cut = buffer.indexOf("\n")) !== -1) {
				const line = buffer.slice(0, cut).replace(/\r$/, "");
				buffer = buffer.slice(cut + 1);
				if (line) take(line);
			}
		});
		void secure;
	});
}

export function startMailServer() {
	const server = net.createServer((socket) => attach(socket));
	server.on("error", (error) => logger.error({ err: error.message }, "mail server failed"));
	server.listen(config.mailPort, config.mailHost, () => {
		logger.info({ host: config.mailHost, port: config.mailPort, domain: config.mailDomain }, "mail server listening");
	});
	return server;
}

function attach(socket) {
	const local = loopback(socket.remoteAddress);
	let buffer = "";
	let mode = "command";
	const envelope = { from: "", rcpt: "" };
	socket.write("220 WhatsApp API\r\n");
	socket.setTimeout(30000, () => socket.destroy());

	socket.on("data", (chunk) => {
		buffer += chunk.toString("utf8");
		if (buffer.length > MAX_BYTES) {
			socket.write("552 Message too large\r\n");
			socket.end();
			return;
		}
		if (mode === "data") {
			const end = buffer.indexOf("\r\n.\r\n");
			if (end === -1) return;
			const raw = buffer.slice(0, end);
			buffer = buffer.slice(end + 5);
			mode = "command";
			accept(envelope, raw)
				.then(() => socket.write("250 OK\r\n"))
				.catch((error) => socket.write(`550 ${error.message}\r\n`));
		}
		let cut;
		while (mode === "command" && (cut = buffer.indexOf("\r\n")) !== -1) {
			const line = buffer.slice(0, cut);
			buffer = buffer.slice(cut + 2);
			const upper = line.toUpperCase();
			if (upper.startsWith("EHLO") || upper.startsWith("HELO")) {
				socket.write("250-bridge\r\n250 OK\r\n");
			} else if (upper.startsWith("MAIL FROM:")) {
				envelope.from = address(line.slice(10));
				socket.write("250 OK\r\n");
			} else if (upper.startsWith("RCPT TO:")) {
				const rcpt = address(line.slice(8));
				const allowed = (local && rcpt) || (currentMail().mailDomain && domain(rcpt) === currentMail().mailDomain);
				if (!allowed) {
					socket.write("550 Relay denied\r\n");
					continue;
				}
				envelope.rcpt = rcpt;
				socket.write("250 OK\r\n");
			} else if (upper === "DATA") {
				if (!envelope.rcpt) {
					socket.write("503 Need a recipient\r\n");
					continue;
				}
				mode = "data";
				socket.write("354 Go ahead\r\n");
			} else if (upper === "RSET") {
				envelope.from = "";
				envelope.rcpt = "";
				socket.write("250 OK\r\n");
			} else if (upper === "NOOP") {
				socket.write("250 OK\r\n");
			} else if (upper === "QUIT") {
				socket.write("221 Bye\r\n");
				socket.end();
			} else {
				socket.write("502 Command not implemented\r\n");
			}
		}
	});
}
