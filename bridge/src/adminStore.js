/** Operator email, saved on disk so it can be set from /admin. */

import fs from "node:fs/promises";
import path from "node:path";

import { config } from "./config.js";

let email = config.adminEmail;

function file() {
	return path.join(path.dirname(config.tenantsFile), "admin.json");
}

export function getAdminEmail() {
	return email;
}

export async function loadStoredAdmin() {
	if (email) return email;
	try {
		const data = JSON.parse(await fs.readFile(file(), "utf8"));
		email = String(data.email || "").trim().toLowerCase();
	} catch (error) {
		if (error.code !== "ENOENT") throw error;
	}
	return email;
}

export async function setAdminEmail(next) {
	const value = String(next || "").trim().toLowerCase();
	if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) {
		const error = new Error("A valid email is required");
		error.statusCode = 400;
		throw error;
	}
	if (email) {
		const error = new Error("An operator email is already set");
		error.statusCode = 409;
		throw error;
	}
	await fs.mkdir(path.dirname(file()), { recursive: true });
	const temporary = `${file()}.tmp`;
	await fs.writeFile(temporary, JSON.stringify({ email: value }));
	await fs.rename(temporary, file());
	email = value;
	return value;
}
