"""Whitelisted endpoints.

These need a real session. An open send endpoint would let anyone use the
linked number to message arbitrary people.
"""

import frappe
from frappe import _


def check_send_permission() -> None:
	if not frappe.has_permission("WhatsApp Message", "read"):
		frappe.throw(_("You are not permitted to send WhatsApp messages."), frappe.PermissionError)


@frappe.whitelist(methods=["POST"])
def send_text(
	to: str,
	message: str,
	session: str | None = None,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
) -> dict:
	"""Send a plain text message."""
	check_send_permission()

	from agent_x.core.messaging import send_message

	return send_message(
		to,
		message,
		session=session,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
	)


@frappe.whitelist(methods=["POST"])
def send_media(
	to: str,
	media_url: str,
	message: str | None = None,
	media_kind: str = "document",
	media_filename: str | None = None,
	session: str | None = None,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
) -> dict:
	"""Send a file or image, optionally with a caption."""
	check_send_permission()

	from agent_x.core.messaging import send_message

	return send_message(
		to,
		message,
		session=session,
		media_url=media_url,
		media_kind=media_kind,
		media_filename=media_filename,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
	)


@frappe.whitelist(methods=["POST"])
def ask(message: str, contact: str | None = None) -> dict:
	"""Run the agent against a message without sending anything.

	This is the safe way to try out a prompt or a policy change: the agent runs
	in full, so any document action it decides on really happens.
	"""
	frappe.only_for("System Manager")

	from agent_x.agent import runtime
	from agent_x.agentx.doctype.whatsapp_contact.whatsapp_contact import get_or_create

	settings = frappe.get_cached_doc("AgentX Settings")

	if contact:
		contact_doc = frappe.get_doc("WhatsApp Contact", contact)
	else:
		# A stand-in contact mapped to whoever is testing, so permissions are real.
		contact_doc = get_or_create("0", "Test Contact")
		if contact_doc.user != frappe.session.user:
			contact_doc.db_set("user", frappe.session.user, update_modified=False)

	result = runtime.handle({"message": message, "message_type": "text"}, contact_doc, settings)

	if not result:
		return {"reply": None, "reason": _("The assistant is disabled.")}

	return result.as_dict()


@frappe.whitelist()
def session_status(session: str | None = None) -> dict:
	"""Live connection state, straight from the bridge."""
	frappe.only_for("System Manager")

	from agent_x.core.transport import get_transport

	return get_transport(session=session).status()


# ---------------------------------------------------------------- rich sends
#
# These wrap provider features beyond plain text. A provider that cannot do one
# answers {"supported": false} rather than raising, so callers can degrade.


@frappe.whitelist(methods=["POST"])
def send_link(to: str, message: str, url: str, session: str | None = None) -> dict:
	"""Send text with a link preview card."""
	check_send_permission()

	from agent_x.core.transport import get_transport

	return get_transport(session=session).send_link(to, message, url)


@frappe.whitelist(methods=["POST"])
def send_location(
	to: str,
	latitude: float,
	longitude: float,
	name: str | None = None,
	address: str | None = None,
	session: str | None = None,
) -> dict:
	"""Drop a pin in the chat."""
	check_send_permission()

	from agent_x.core.transport import get_transport

	return get_transport(session=session).send_location(
		to, float(latitude), float(longitude), name=name, address=address
	)


@frappe.whitelist(methods=["POST"])
def send_poll(
	to: str,
	question: str,
	options: list | str,
	multiple: bool = False,
	session: str | None = None,
) -> dict:
	"""Ask a question with tappable answers."""
	check_send_permission()

	from agent_x.core.transport import get_transport

	if isinstance(options, str):
		options = frappe.parse_json(options)

	return get_transport(session=session).send_poll(to, question, options, bool(multiple))


@frappe.whitelist(methods=["POST"])
def react(
	chat_id: str, message_id: str, emoji: str, from_me: bool = False, session: str | None = None
) -> dict:
	"""Put an emoji on a message."""
	check_send_permission()

	from agent_x.core.transport import get_transport

	return get_transport(session=session).react(chat_id, message_id, emoji, bool(from_me))


@frappe.whitelist(methods=["POST"])
def check_numbers(numbers: list | str, session: str | None = None) -> dict:
	"""Which of these numbers are actually on WhatsApp."""
	check_send_permission()

	from agent_x.core.transport import get_transport

	if isinstance(numbers, str):
		numbers = frappe.parse_json(numbers) if numbers.strip().startswith("[") else [numbers]

	transport = get_transport(session=session)
	if hasattr(transport, "check_numbers"):
		return transport.check_numbers(numbers)

	return {"results": [{"number": n, **transport.check_number(n)} for n in numbers]}


@frappe.whitelist()
def get_groups(session: str | None = None) -> dict:
	"""Groups the linked number belongs to, with their chat ids."""
	frappe.only_for("System Manager")

	from agent_x.core.transport import get_transport

	return get_transport(session=session).get_groups()


@frappe.whitelist()
def get_chats(limit: int = 50, session: str | None = None) -> dict:
	"""Recent chats, for finding a chat id."""
	frappe.only_for("System Manager")

	from agent_x.core.transport import get_transport

	return get_transport(session=session).get_chats(int(limit))


@frappe.whitelist(methods=["POST"])
def send_document(
	to: str,
	doctype: str,
	name: str,
	message: str | None = None,
	print_format: str | None = None,
	session: str | None = None,
) -> dict:
	"""Send a PDF of a document, using the site's print format for it."""
	check_send_permission()

	if not frappe.has_permission(doctype, "read", doc=name):
		frappe.throw(
			_("You are not permitted to read {0} {1}.").format(doctype, name), frappe.PermissionError
		)

	from agent_x.core import printing
	from agent_x.core.messaging import send_message
	from agent_x.core.transport import get_transport

	settings = frappe.get_cached_doc("AgentX Settings")
	transport = get_transport(session=session, settings=settings)

	prepared = printing.prepare(
		doctype,
		name,
		needs_public_url=transport.needs_public_media,
		print_format=print_format,
	)

	result = send_message(
		to,
		message or _("Here is {0} {1}.").format(doctype, name),
		session=session,
		media_url=prepared.get("url"),
		media_base64=prepared.get("base64"),
		media_kind="document",
		media_filename=prepared["filename"],
		media_mimetype="application/pdf",
		reference_doctype=doctype,
		reference_name=name,
		settings=settings,
	)

	return {**result, "print_format": prepared["print_format"], "filename": prepared["filename"]}

