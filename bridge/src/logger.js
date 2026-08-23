import pino from "pino";

import { config } from "./config.js";

export const logger = pino({ level: config.logLevel });

// Baileys is extremely chatty at debug level; keep its own logger quiet
// unless the operator explicitly asked for trace output.
export const baileysLogger = pino({ level: config.logLevel === "trace" ? "debug" : "silent" });
