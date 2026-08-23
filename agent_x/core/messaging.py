"""Sending messages, and logging both directions."""

import frappe
from frappe import _
from frappe.utils import now_datetime

from agent_x.core.phone import digits_only, normalise
from agent_x.core.transport import get_transport
from agent_x.agentx.doctype.whatsapp_contact.whatsapp_contact import get_or_create, record_activity


def resolve_session(session: str | None, settings) -> str:
	from agent_x.agentx.doctype.whatsapp_session.whatsapp_session import get_default_session

	name = session or settings.default_session or get_default_session()
	if not name:
		frappe.throw(_("No WhatsApp session is connected. Pair a number first."))
	return name


def send_message(
	to: str,
	message: str | None = None,
	*,
	session: str | None = None,
	media_url: str | None = None,
	media_base64: str | None = None,
	media_kind: str = "document",
	media_filename: str | None = None,
	media_mimetype: str | None = None,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	agent_run: str | None = None,
	settings=None,
) -> dict:
	"""Send one message and record it."""
	settings = settings or frappe.get_cached_doc("AgentX Settings")

	if not settings.enabled:
		frappe.throw(_("AgentX is disabled in AgentX Settings."))

	if not (message or media_url or media_base64):
		frappe.throw(_("A message needs text or an attachment."))

	number = normalise(to, settings.default_country_code)
	if not number:
		frappe.throw(_("{0} is not a usable phone number.").format(to))

	session_name = resolve_session(session, settings)
	contact = get_or_create(number)

	if contact.blocked:
		frappe.throw(_("{0} is blocked, so nothing was sent.").format(number))

	log = log_outgoing(
		settings,
		session_name,
		contact,
		number,
		message,
		media_filename=media_filename,
		media_mimetype=media_mimetype,
		media_kind=media_kind if (media_url or media_base64) else "text",
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		agent_run=agent_run,
	)

	transport = get_transport(session=session_name, settings=settings)

	try:
		if media_url or media_base64:
			result = transport.send_media(
				number,
				url=media_url,
				base64_content=media_base64,
				kind=media_kind,
				mimetype=media_mimetype,
				filename=media_filename,
				caption=message,
			)
		else:
			result = transport.send_text(number, message)

	except Exception as exc:
		if log:
			log.db_set({"status": "Failed", "error": str(exc)[:500]}, update_modified=False)
		raise

	if log:
		log.db_set(
			{"status": "Sent", "message_id": result.get("message_id"), "sent_on": now_datetime()},
			update_modified=False,
		)

	record_activity(contact, "Outgoing")
	bump_session(session_name, "messages_sent")

	return {
		"sent": True,
		"to": number,
		"session": session_name,
		"message_id": result.get("message_id"),
		"log": log.name if log else None,
	}


def log_outgoing(settings, session, contact, number, message, **kw):
	if not settings.log_messages:
		return None

	doc = frappe.get_doc(
		{
			"doctype": "WhatsApp Message",
			"direction": "Outgoing",
			"status": "Queued",
			"session": session,
			"contact": contact.name,
			"wa_id": number,
			"message_type": kw.get("media_kind") or "text",
			"message": message,
			"media_filename": kw.get("media_filename"),
			"media_mimetype": kw.get("media_mimetype"),
			"reference_doctype": kw.get("reference_doctype"),
			"reference_name": kw.get("reference_name"),
			"agent_run": kw.get("agent_run"),
			"sent_on": now_datetime(),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def bump_session(session: str, field: str) -> None:
	"""Counters are advisory, so a failure here must not break a send."""
	try:
		frappe.db.sql(
			f"UPDATE `tabWhatsApp Session` SET `{field}` = COALESCE(`{field}`, 0) + 1 WHERE name = %s",
			session,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "AgentX: session counter update failed")
