"""A lesson from a reply that went wrong.

Corrections are stated as overriding instructions and placed last in the system
prompt, because they exist precisely to override behaviour the earlier sections
produced.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class AgentCorrection(Document):
	pass


def get_active(limit: int = 20, document_type: str | None = None) -> list[dict]:
	"""The corrections worth spending prompt space on."""
	filters = {"enabled": 1}

	rows = frappe.get_all(
		"Agent Correction",
		filters=filters,
		fields=["name", "applies_when", "wrong_reply", "correct_behaviour", "document_type"],
		order_by="priority desc, modified desc",
		limit=max(1, limit),
	)

	# A correction tied to a doctype only applies to that one; untied ones always do.
	return [r for r in rows if not r.document_type or r.document_type == document_type]


def record_applied(names: list[str]) -> None:
	"""Count usage, so it is visible which lessons are actually earning their place."""
	if not names:
		return

	try:
		for name in names:
			frappe.db.set_value(
				"Agent Correction",
				name,
				{"times_applied": (frappe.db.get_value("Agent Correction", name, "times_applied") or 0) + 1,
				 "last_applied_on": now_datetime()},
				update_modified=False,
			)
	except Exception:
		# Bookkeeping must never break a reply.
		frappe.log_error(frappe.get_traceback(), "AgentX: could not record correction usage")


def format_for_prompt(corrections: list[dict]) -> str:
	"""Render corrections as explicit do-not-repeat instructions."""
	if not corrections:
		return ""

	lines = [
		"\n--- CORRECTIONS FROM PAST MISTAKES ---",
		"These are real mistakes you made before. Each one overrides the general rules "
		"and the business context above. Do not repeat them.",
	]

	for index, row in enumerate(corrections, 1):
		lines.append(f"\n{index}. When: {row['applies_when']}")
		if row.get("wrong_reply"):
			lines.append(f"   You wrongly said: {row['wrong_reply']}")
		lines.append(f"   Do this instead: {row['correct_behaviour']}")

	return "\n".join(lines)
