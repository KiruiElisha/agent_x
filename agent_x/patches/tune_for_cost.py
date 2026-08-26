"""Lower the settings that drive the model bill, where they were never chosen.

Each tool step is a full call carrying the whole system prompt and every tool
definition, measured at roughly 2,900 tokens before the conversation itself.
Six steps was generous; four covers the work and removes a third of the worst
case.

Only values still sitting on the old defaults are touched. Anything an operator
deliberately set is left alone, because a number they picked is a decision and
this is not.
"""

import frappe

# field: (the old default we shipped, what to move it to)
TUNING = {
	"max_tool_iterations": (6, 4),
	"history_limit": (20, 12),
	"ai_max_output_tokens": (2048, 4096),
}


def execute() -> None:
	if not frappe.db.exists("DocType", "AgentX Settings"):
		return

	settings = frappe.get_single("AgentX Settings")
	changed = []

	for field, (was, now) in TUNING.items():
		if not settings.meta.has_field(field):
			continue

		current = settings.get(field)
		if current in (None, "", 0, was):
			settings.set(field, now)
			changed.append(f"{field} {current} -> {now}")

	if not changed:
		return

	settings.flags.ignore_permissions = True
	settings.flags.ignore_validate = True
	settings.save()
	frappe.db.commit()

	print("AgentX: " + "; ".join(changed))
