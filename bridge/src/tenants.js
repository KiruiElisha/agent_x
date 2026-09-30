/** Tenant tokens. The master token is admin; every other token owns its sessions. */

import crypto from "node:crypto";
import fs from "node:fs/promises";

import { config } from "./config.js";

let queue = Promise.resolve();

function locked(work) {
	const run = queue.then(work, work);
	queue = run.then(
		() => undefined,
		() => undefined,
	);
	return run;
}

async function read() {
	try {
		const raw = await fs.readFile(config.tenantsFile, "utf8");
		const data = JSON.parse(raw);
		return data && typeof data.tenants === "object" && data.tenants ? data.tenants : {};
	} catch (error) {
		if (error.code === "ENOENT") return {};
		throw error;
	}
}

async function write(tenants) {
	const body = JSON.stringify({ tenants }, null, 2);
	const temporary = `${config.tenantsFile}.tmp`;
	await fs.writeFile(temporary, body);
	await fs.rename(temporary, config.tenantsFile);
}

function same(a, b) {
	const left = Buffer.from(String(a));
	const right = Buffer.from(String(b));
	if (left.length !== right.length) return false;
	return crypto.timingSafeEqual(left, right);
}

export function assertTenantId(id) {
	if (!/^[a-z0-9][a-z0-9._-]{0,31}$/.test(String(id || ""))) {
		throw new Error(
			"Tenant id must be 1-32 characters: a lowercase letter or digit, then letters, digits, dot, dash, or underscore",
		);
	}
	return id;
}

export async function createTenant(id, email = "") {
	assertTenantId(id);
	return locked(async () => {
		const tenants = await read();
		if (tenants[id]) {
			const error = new Error(`Tenant ${id} already exists`);
			error.statusCode = 409;
			throw error;
		}
		const token = crypto.randomBytes(32).toString("hex");
		tenants[id] = { token, email: email || "", created_at: new Date().toISOString() };
		await write(tenants);
		return { id, token, email: email || "", created_at: tenants[id].created_at };
	});
}

export async function listTenants() {
	const tenants = await read();
	return Object.entries(tenants).map(([id, row]) => ({
		id,
		created_at: row.created_at || null,
	}));
}

export async function deleteTenant(id) {
	assertTenantId(id);
	return locked(async () => {
		const tenants = await read();
		if (!tenants[id]) return false;
		delete tenants[id];
		await write(tenants);
		return true;
	});
}

/** Master token is admin. A stored tenant token is that tenant. Anything else is refused. */
export async function resolveAccess(token) {
	if (!token) return null;
	if (same(token, config.apiToken)) return { role: "admin" };

	const tenants = await read();
	for (const [id, row] of Object.entries(tenants)) {
		if (row?.token && same(token, row.token)) return { role: "tenant", id };
	}
	return null;
}
