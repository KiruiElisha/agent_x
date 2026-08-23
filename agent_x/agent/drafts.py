"""Sending a freshly created draft back for review.

A twenty line order is not really checkable as a chat message. Once the draft
exists, the print format says exactly what was recorded, so send that and let
the customer look before anything is submitted.
"""

import frappe
from frappe import _


def line_count(doctype: str, name: str) -> int:
	"""How many rows the biggest child table on this document has."""
	try:
		doc = frappe.get_doc(doctype, name)
	except Exception:
		return 0

	biggest = 0
	for field in doc.meta.fields:
		if field.fieldtype == "Table":
			biggest = max(biggest, len(doc.get(field.fieldname) or []))

	return biggest


def should_send(action, settings) -> bool:
	"""Only for something just created, and only when it is long enough to matter."""
	threshold = settings.send_draft_after_lines or 0
	if threshold <= 0:
		return False

	if action.action != "create" or not action.document_name:
		return False

	if not (settings.allow_document_pdfs and settings.automation_enabled):
		return False

	return line_count(action.document_type, action.document_name) >= threshold


def maybe_send(action, settings, contact, session: str | None, run: str | None = None) -> dict | None:
	"""Send the draft as a PDF, if it is the kind of thing worth reviewing.

	Best effort: the document is already created, so failing to send a copy of
	it must not read as the creation having failed.
	"""
	if not contact:
		return None

	try:
		if not should_send(action, settings):
			return None
	except Exception:
		frappe.log_error(frappe.get_traceback(), "AgentX: could not size the draft")
		return None

	from agent_x.agentx.doctype.agent_action.agent_action import switch_user
	from agent_x.core import printing
	from agent_x.core.messaging import send_message
	from agent_x.core.transport import get_transport

	try:
		transport = get_transport(session=session, settings=settings)

		# Render as the user who created it, so the print format sees what they see.
		with switch_user(action.acting_user):
			prepared = printing.prepare(
				action.document_type,
				action.document_name,
				needs_public_url=transport.needs_public_media,
				print_format=print_format_for(action, settings),
			)

		return send_message(
			contact.wa_id,
			_("Here is the draft {0} {1}. Please check it before we go ahead.").format(
				action.document_type, action.document_name
			),
			session=session,
			media_url=prepared.get("url"),
			media_base64=prepared.get("base64"),
			media_kind="document",
			media_filename=prepared["filename"],
			media_mimetype="application/pdf",
			reference_doctype=action.document_type,
			reference_name=action.document_name,
			agent_run=run,
			settings=settings,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "AgentX: could not send the draft for review")
		return None


def print_format_for(action, settings) -> str | None:
	for row in settings.doctype_policies:
		if row.document_type == action.document_type:
			return row.print_format or None
	return None
