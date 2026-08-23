"""Ongoing conversation state for one contact."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class AgentConversation(Document):
	def settings(self):
		return frappe.get_cached_doc("AgentX Settings")

	@frappe.whitelist()
	def take_over(self, reason: str | None = None) -> dict:
		"""Silence the assistant and handle this conversation yourself."""
		from agent_x.agent import handoff

		if not frappe.has_permission("Agent Conversation", "write", doc=self):
			frappe.throw(_("You are not permitted to take over conversations."), frappe.PermissionError)

		return handoff.start(
			self,
			self.settings(),
			reason=reason or _("Taken over by {0}.").format(frappe.session.user),
			by=frappe.session.user,
		)

	@frappe.whitelist()
	def give_back(self) -> dict:
		"""Return the conversation to the assistant."""
		from agent_x.agent import handoff

		if not frappe.has_permission("Agent Conversation", "write", doc=self):
			frappe.throw(_("You are not permitted to change conversations."), frappe.PermissionError)

		return handoff.finish(self, self.settings())

	@frappe.whitelist()
	def send_reply(self, message: str) -> dict:
		"""Reply as a person, from the desk.

		Sending does not on its own take the conversation over. If a person is
		typing, though, the assistant should not be answering too, so a reply
		while the assistant is active hands it over first.
		"""
		if not frappe.has_permission("WhatsApp Message", "create"):
			frappe.throw(_("You are not permitted to send WhatsApp messages."), frappe.PermissionError)

		text = (message or "").strip()
		if not text:
			frappe.throw(_("Write something to send."))

		settings = self.settings()

		if self.status != "Handed Over":
			self.take_over(reason=_("{0} replied directly.").format(frappe.session.user))
			self.reload()

		from agent_x.core.messaging import send_message

		wa_id = frappe.db.get_value("WhatsApp Contact", self.contact, "wa_id")
		result = send_message(wa_id, text, session=self.session, settings=settings)

		self.db_set("last_message_on", now_datetime(), update_modified=False)
		return result

	@frappe.whitelist()
	def thread(self, limit: int = 30) -> list[dict]:
		"""Recent messages with this contact, newest last."""
		rows = frappe.get_all(
			"WhatsApp Message",
			filters={"contact": self.contact},
			fields=["name", "direction", "message", "status", "creation", "message_type", "alert"],
			order_by="creation desc",
			limit=max(1, min(int(limit or 30), 100)),
		)
		return list(reversed(rows))


def get_or_create(contact: str, session: str | None = None, acting_user: str | None = None) -> Document:
	"""The open conversation for a contact, or a new one."""
	name = frappe.db.get_value(
		"Agent Conversation",
		{"contact": contact, "status": ("in", ("Active", "Awaiting Confirmation", "Handed Over"))},
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
