/** Bridge entry point. */

import path from "node:path";
import { fileURLToPath } from "node:url";

import express from "express";

import { config } from "./config.js";
import { logger } from "./logger.js";
import { buildRouter, errorHandler } from "./routes.js";
import { SessionManager } from "./session.js";

const publicDir = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "public");

const manager = new SessionManager();
const app = express();

app.use(express.json({ limit: "25mb" }));

// Unauthenticated, so a process manager can check liveness.
app.get("/health", (req, res) => res.json({ ok: true, sessions: manager.list().length }));

// The onboarding page. A missing file used to fall through to "Cannot GET /".
app.get(["/", "/index.html"], (_req, res) => {
	res.sendFile(path.join(publicDir, "index.html"), (error) => {
		if (error) {
			logger.error({ err: error.message }, "onboarding page is not in this install");
			res.status(500).type("text/plain").send("The onboarding page is missing from this bridge install.");
		}
	});
});

app.use("/api", buildRouter(manager));
app.use(errorHandler);

const LOOPBACK = new Set(["127.0.0.1", "::1", "localhost"]);

const server = app.listen(config.port, config.host, async () => {
	logger.info({ host: config.host, port: config.port }, "bridge listening");
	// In a container 0.0.0.0 is required, and the port mapping decides exposure.
	if (!LOOPBACK.has(config.host) && !process.env.BRIDGE_IN_CONTAINER) {
		// Anyone who can reach this port and guess the token can send from the
		// linked number. Put it behind a firewall or a TLS proxy.
		logger.warn({ host: config.host }, "bridge is listening beyond loopback");
	}
	await manager.restoreAll();
});

// Baileys can take a while to close its sockets; supervisor should not wait forever.
const SHUTDOWN_GRACE_MS = 10000;

async function shutdown(signal, code = 0) {
	logger.info({ signal }, "shutting down");
	setTimeout(() => process.exit(code), SHUTDOWN_GRACE_MS).unref();
	server.close();
	try {
		await manager.shutdown();
	} catch (error) {
		logger.error({ err: String(error) }, "error while closing sessions");
	}
	process.exit(code);
}

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));

// A dropped socket inside Baileys should not take the whole bridge down.
process.on("unhandledRejection", (reason) =>
	logger.error({ err: String(reason) }, "unhandled rejection"),
);

// A thrown exception leaves the process in an unknown state. Close what we can
// and exit, so the process manager starts a clean one; credentials on disk
// mean it reconnects without a new QR.
process.on("uncaughtException", (error) => {
	logger.fatal({ err: error?.stack || String(error) }, "uncaught exception");
	shutdown("uncaughtException", 1);
});
