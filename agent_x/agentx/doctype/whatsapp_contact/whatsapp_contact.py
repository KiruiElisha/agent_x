"""People and groups we exchange messages with."""

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class WhatsAppContact(Document):
	def validate(self) -> None:
		from agent_x.core.phone import digits_only

		if not self.is_group:
			self.wa_id = digits_only(self.wa_id) or self.wa_id

		if not self.contact_name:
			self.contact_name = self.push_name or self.wa_id


def get_or_create(wa_id: str, push_name: str | None = None, is_group: bool = False) -> Document:
	"""Find the contact for a number, creating it the first time we see them."""
	if frappe.db.exists("WhatsApp Contact", wa_id):
		doc = frappe.get_doc("WhatsApp Contact", wa_id)

		# WhatsApp profile names change; keep ours current but never overwrite a
		# name a human typed.
		if push_name and doc.push_name != push_name:
			updates = {"push_name": push_name}
			if not doc.contact_name or doc.contact_name == doc.push_name or doc.contact_name == doc.wa_id:
				updates["contact_name"] = push_name
			doc.db_set(updates, update_modified=False)

		return doc

	doc = frappe.get_doc(
		{
			"doctype": "WhatsApp Contact",
			"wa_id": wa_id,
			"push_name": push_name,
			"contact_name": push_name or wa_id,
			"is_group": 1 if is_group else 0,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def record_activity(contact: Document, direction: str) -> None:
	"""Stamp the last message time without bumping `modified`."""
	field = "last_incoming_on" if direction == "Incoming" else "last_outgoing_on"

	contact.db_set(
		{field: now_datetime(), "message_count": (contact.message_count or 0) + 1},
		update_modified=False,
	)


def acting_user(contact: Document, settings) -> str | None:
	"""Which Frappe user this contact's document actions run as.

	The contact's own mapping wins, then the allowed-numbers table, then the
	configured default. Returns None when the contact may not act at all.
	"""
	if contact.user:
		return contact.user

	mapped = settings.user_for_number(contact.wa_id)
	if mapped:
		return mapped

	if settings.require_user_mapping:
		return None

	return settings.agent_user or None
