"""Handing a conversation to a person, and giving it back.

Three ways in: the customer asks for a human, the assistant decides it cannot
help, or a member of staff takes over from the desk. While a conversation is
handed over the assistant says nothing at all — two voices answering one person
is worse than a slow reply.
"""

import re

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

DEFAULT_KEYWORDS = "human,agent,person,someone,representative,talk to someone,customer care"


def keywords(settings) -> list[str]:
	raw = settings.handoff_keywords or DEFAULT_KEYWORDS
	return [k.strip().casefold() for k in raw.split(",") if k.strip()]


def asked_for_a_person(text: str, settings) -> bool:
	"""Whether this message is a request for a human.

	Matched on whole words, so "person" does not fire on "personalised" and
	"agent" does not fire on the assistant talking about itself.
	"""
	if not settings.handoff_enabled:
		return False

	cleaned = (text or "").strip().casefold()
	if not cleaned or len(cleaned) > 200:
		# A long message is a question, not a request to escalate.
		return False

	for keyword in keywords(settings):
		if re.search(rf"\b{re.escape(keyword)}\b", cleaned):
			return True

	return False


def start(conversation, settings, reason: str | None = None, by: str | None = None) -> dict:
	"""Put a conversation in a person's hands."""
	minutes = settings.handoff_timeout_minutes or 0
	expires = add_to_date(now_datetime(), minutes=minutes) if minutes > 0 else None

	conversation.db_set(
		{
			"status": "Handed Over",
			"handed_over_on": now_datetime(),
			"handover_reason": (reason or "")[:500] or None,
			"handover_expires_on": expires,
			# A pending change should not survive a handover: the person taking
			# over decides what happens next, not a stale confirmation.
			"pending_action": None,
			"pending_expires_on": None,
		},
		update_modified=False,
	)

	notify(conversation, settings, reason, by)
	frappe.db.commit()

	return {"status": "Handed Over", "expires_on": expires}


def finish(conversation, settings=None) -> dict:
	"""Give the conversation back to the assistant."""
	conversation.db_set(
		{
			"status": "Active",
			"handover_reason": None,
			"handover_expires_on": None,
		},
		update_modified=False,
	)
	frappe.db.commit()
	return {"status": "Active"}


def is_held(conversation, settings) -> bool:
	"""Whether a person still has this conversation."""
	if conversation.status != "Handed Over":
		return False

	expires = conversation.handover_expires_on
	if expires and now_datetime() > expires:
		# Nobody picked it up, so the assistant takes it back rather than
		# leaving the customer with silence.
		finish(conversation, settings)
		return False

	return True


def notify(conversation, settings, reason: str | None, by: str | None) -> None:
	"""Tell somebody a person is needed. Never blocks the handover."""
	recipients = staff(settings)
	if not recipients:
		return

	contact_name = frappe.db.get_value("WhatsApp Contact", conversation.contact, "contact_name")
	subject = _("WhatsApp: {0} needs a person").format(contact_name or conversation.contact)

	for user in recipients:
		try:
			todo = frappe.get_doc(
				{
					"doctype": "ToDo",
					"allocated_to": user,
					"reference_type": "Agent Conversation",
					"reference_name": conversation.name,
					"description": subject + (f"\n\n{reason}" if reason else ""),
					"priority": "High",
				}
			)
			todo.insert(ignore_permissions=True)

			frappe.publish_realtime(
				"agentx_handoff",
				{
					"conversation": conversation.name,
					"contact": contact_name or conversation.contact,
					"reason": reason,
				},
				user=user,
			)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "AgentX: could not notify about a handover")


def staff(settings) -> list[str]:
	"""Who gets told. A named user wins over a role."""
	if settings.handoff_user:
		return [settings.handoff_user]

	if not settings.handoff_role:
		return []

	return frappe.get_all(
		"Has Role",
		filters={"role": settings.handoff_role, "parenttype": "User"},
		pluck="parent",
		limit=20,
	)


def release_expired() -> None:
	"""Return conversations nobody picked up. Scheduled hourly."""
	settings = frappe.get_cached_doc("AgentX Settings")

	stale = frappe.get_all(
		"Agent Conversation",
		# A "<" comparison already excludes NULL, so no separate is-set check.
		filters={"status": "Handed Over", "handover_expires_on": ("<", now_datetime())},
		pluck="name",
	)

	for name in stale:
		try:
			finish(frappe.get_doc("Agent Conversation", name), settings)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "AgentX: could not release a handover")
