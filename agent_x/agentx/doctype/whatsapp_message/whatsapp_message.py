"""A single WhatsApp message, in either direction."""

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, now_datetime


class WhatsAppMessage(Document):
	pass


def delete_old_logs() -> None:
	"""Trim the message log. Scheduled daily."""
	settings = frappe.get_cached_doc("AgentX Settings")
	days = settings.log_retention_days or 0
	if days <= 0:
		return

	cutoff = add_days(now_datetime(), -days)

	# Delete in batches so a large backlog does not hold one long transaction.
	while True:
		names = frappe.get_all(
			"WhatsApp Message",
			filters={"creation": ("<", cutoff)},
			pluck="name",
			limit=500,
		)
		if not names:
			break

		frappe.db.delete("WhatsApp Message", {"name": ("in", names)})
		frappe.db.commit()
