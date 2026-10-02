/** Operator commands for the installed bridge. Only the fixed list is allowed. */

import { spawn } from "node:child_process";

const ALLOWED = new Set(["status", "logs", "restart", "stop", "start", "backup", "update"]);

export function runControl(command) {
	if (!ALLOWED.has(command)) {
		const error = new Error("That bridge command is not available");
		error.statusCode = 400;
		throw error;
	}
	const args = command === "logs" ? ["logs", "80", "--no-follow"] : [command];
	const timeout = command === "update" ? 180000 : 40000;
	return new Promise((resolve, reject) => {
		const child = spawn("agentx-bridge", args, { timeout });
		let output = "";
		const take = (chunk) => {
			output += chunk.toString("utf8");
			if (output.length > 20000) output = output.slice(-20000);
		};
		child.stdout?.on("data", take);
		child.stderr?.on("data", take);
		child.on("error", (error) => {
			if (error.code === "ENOENT") {
				const missing = new Error("agentx-bridge is not installed on this server");
				missing.statusCode = 400;
				reject(missing);
				return;
			}
			reject(error);
		});
		child.on("close", (code) => {
			if (code && code !== 0) {
				const error = new Error((output || `agentx-bridge ${command} failed`).trim());
				error.statusCode = 400;
				reject(error);
				return;
			}
			resolve({ command, output: output.trim() });
		});
	});
}
