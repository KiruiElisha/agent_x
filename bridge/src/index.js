/** Bridge entry point. */

import express from "express";

import { config } from "./config.js";
import { logger } from "./logger.js";
import { buildRouter, errorHandler } from "./routes.js";
import { SessionManager } from "./session.js";

const manager = new SessionManager();
const app = express();

app.use(express.json({ limit: "25mb" }));

// Unauthenticated, so a process manager can check liveness.
app.get("/health", (req, res) => res.json({ ok: true, sessions: manager.list().length }));

app.use("/api", buildRouter(manager));
app.use(errorHandler);

const server = app.listen(config.port, config.host, async () => {
	logger.info({ host: config.host, port: config.port }, "bridge listening");
	await manager.restoreAll();
});

async function shutdown(signal) {
	logger.info({ signal }, "shutting down");
	server.close();
	await manager.shutdown();
	process.exit(0);
}

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));

// A dropped socket inside Baileys should not take the whole bridge down.
process.on("unhandledRejection", (reason) =>
	logger.error({ err: String(reason) }, "unhandled rejection"),
);
