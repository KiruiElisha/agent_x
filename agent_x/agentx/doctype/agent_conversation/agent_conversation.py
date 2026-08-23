"""Ongoing conversation state for one contact."""

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class AgentConversation(Document):
	pass


def get_or_create(contact: str, session: str | None = None, acting_user: str | None = None) -> Document:
	"""The open conversation for a contact, or a new one."""
	name = frappe.db.get_value(
		"Agent Conversation",
		{"contact": contact, "status": ("in", ("Active", "Awaiting Confirmation"))},
		"name",
	)
	if name:
		doc = frappe.get_doc("Agent Conversation", name)
		if session and doc.session != session:
			doc.db_set("session", session, update_modified=False)
		return doc

	doc = frappe.get_doc(
		{
			"doctype": "Agent Conversation",
			"contact": contact,
			"session": session,
			"acting_user": acting_user,
			"status": "Active",
			"last_message_on": now_datetime(),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc
