"""Receives inbound events from whichever WhatsApp provider is configured.

Two providers, two shapes:

  Self-hosted bridge — already sends the flat shape AgentX uses, and signs every
  post with an HMAC of the body.

  WaClient — forwards Baileys output more or less raw, and cannot sign, so it
  authenticates with a shared token on the URL instead.

Always answers 200 unless authentication fails. A webhook that returns errors
gets retried or switched off by the sender, which loses more messages than
quietly dropping one bad event.
"""

import hashlib
import hmac
import json

import frappe
from frappe import _
from frappe.utils import now_datetime

from agent_x.agentx.doctype.whatsapp_contact.whatsapp_contact import get_or_create, record_activity
from agent_x.agentx.doctype.whatsapp_session import whatsapp_session
from agent_x.core import payload as payload_parser

BRIDGE = "Self-Hosted Bridge"
WACLIENT = "WaClient"

# Baileys ack codes, as the bridge forwards them.
RECEIPT_STATUS = {1: "Sent", 2: "Sent", 3: "Delivered", 4: "Read", 5: "Read", 0: "Failed", -1: "Failed"}

SESSION_EVENTS = ("qr", "qr_expired", "connected", "disconnected", "logged_out")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive() -> dict:
	"""Handle one inbound event."""
	settings = frappe.get_cached_doc("AgentX Settings")

	if not settings.enabled:
		return {"status": "ignored", "reason": "AgentX is disabled"}

	raw = frappe.request.get_data() if frappe.request else b""
	provider = settings.whatsapp_provider or WACLIENT

	if not authenticate(settings, provider, raw):
		frappe.local.response["http_status_code"] = 401
		return {"status": "unauthorised"}

	body = read_json(raw)
	if body is None:
		return {"status": "ignored", "reason": "unreadable payload"}

	try:
		if provider == BRIDGE:
			return dispatch_bridge(body, settings)
		return dispatch_waclient(body, settings)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			f"{frappe.get_traceback()}\n\nPayload:\n{json.dumps(body, default=str)[:3000]}",
			"AgentX webhook failed",
		)
		return {"status": "error"}


def read_json(raw: bytes) -> dict | None:
	try:
		data = json.loads(raw or b"{}")
		if isinstance(data, dict):
			return data
	except ValueError:
		pass

	# Some providers post form-encoded bodies instead of JSON.
	form = {k: v for k, v in frappe.form_dict.items() if k not in ("cmd", "token")}
	return form or None


# ------------------------------------------------------------------ auth


def authenticate(settings, provider: str, raw: bytes) -> bool:
	if provider == BRIDGE:
		return verify_signature(settings, raw)
	return verify_token(settings)


def verify_signature(settings, raw: bytes) -> bool:
	"""HMAC of the raw body, as the bridge sends it."""
	if not settings.verify_signature:
		return True

	secret = settings.get_password("webhook_secret", raise_exception=False)
	if not secret:
		# Verification is on but there is nothing to check against. Refuse,
		# rather than silently accepting anything, which is what an attacker wants.
		frappe.log_error(
			"Signature verification is on but no Webhook Secret is set.",
			"AgentX webhook misconfigured",
		)
		return False

	provided = frappe.get_request_header("X-AgentX-Signature") or ""
	if provided.startswith("sha256="):
		provided = provided[7:]

	expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
	return hmac.compare_digest(provided, expected)


def verify_token(settings) -> bool:
	"""Shared token, for providers that cannot sign their posts."""
	expected = settings.get_password("webhook_token", raise_exception=False)
	if not expected:
		# No token configured means the endpoint is open. That is a real risk,
		# so say so once rather than failing silently either way.
		frappe.log_error(
			"No Webhook Token is set, so the inbound endpoint accepts anything. "
			"Set one in AgentX Settings and re-register the webhook.",
			"AgentX webhook is unauthenticated",
		)
		return True

	provided = (
		frappe.form_dict.get("token")
		or frappe.get_request_header("X-Webhook-Token")
		or frappe.get_request_header("X-Token")
		or ""
	)
	return hmac.compare_digest(str(provided), str(expected))


# ------------------------------------------------------- bridge (flat shape)


def dispatch_bridge(body: dict, settings) -> dict:
	event = str(body.get("event") or "").strip()
	session = body.get("session")

	if not event:
		return {"status": "ignored", "reason": "no event name"}

	if event in SESSION_EVENTS:
		whatsapp_session.handle_event(session, event, body)
		return {"status": "ok", "handled": event}

	if event == "receipt":
		return apply_receipt(body.get("message_id"), RECEIPT_STATUS.get(body.get("status")))

	if event == "message":
		data = body.get("message") or {}
		return ingest(
			settings,
			session=session,
			wa_id=data.get("wa_id"),
			from_me=data.get("from_me"),
			is_group=data.get("is_group"),
			push_name=data.get("push_name"),
			text=data.get("text"),
			message_type=data.get("message_type"),
			media=data.get("media") or {},
			message_id=data.get("message_id"),
			chat_id=data.get("chat_id"),
			timestamp=data.get("timestamp"),
			raw=body,
		)

	return {"status": "ignored", "reason": f"unknown event {event}"}


# --------------------------------------------------- waclient (nested shape)


def dispatch_waclient(body: dict, settings) -> dict:
	parsed = payload_parser.parse(body)
	if not parsed:
		return {"status": "ignored", "reason": "no message in payload"}

	# Delivery receipts arrive on the same endpoint as messages.
	if payload_parser.is_status_event(parsed):
		status = payload_parser.extract_ack(body, parsed)
		if status:
			return apply_receipt(parsed.get("message_id"), status)

	if not parsed.get("wa_id"):
		return {"status": "ignored", "reason": "no sender"}

	session = session_for_instance(parsed.get("instance_id"))

	return ingest(
		settings,
		session=session,
		wa_id=parsed.get("wa_id"),
		from_me=parsed.get("from_me"),
		is_group=parsed.get("is_group"),
		push_name=parsed.get("push_name"),
		text=parsed.get("text"),
		message_type=parsed.get("message_type"),
		media=parsed.get("media") or {},
		message_id=parsed.get("message_id"),
		chat_id=parsed.get("chat_id"),
		timestamp=parsed.get("timestamp"),
		raw=body,
	)


def session_for_instance(instance_id: str | None) -> str | None:
	if not instance_id:
		return None
	return frappe.db.get_value("WhatsApp Session", {"instance_id": instance_id}, "name")


# ------------------------------------------------------------------ shared


def apply_receipt(message_id: str | None, status: str | None) -> dict:
	if not (message_id and status):
		return {"status": "ignored", "reason": "incomplete receipt"}

	name = frappe.db.get_value(
		"WhatsApp Message", {"message_id": message_id, "direction": "Outgoing"}, "name"
	)
	if not name:
		return {"status": "ignored", "reason": "no matching message"}

	frappe.db.set_value("WhatsApp Message", name, "status", status, update_modified=False)
	frappe.db.commit()
	return {"status": "ok", "handled": "receipt"}


def ingest(settings, **event) -> dict:
	"""Log one inbound message and, unless something says not to, answer it."""
	wa_id = event.get("wa_id")
	if not wa_id:
		return {"status": "ignored", "reason": "no sender"}

	# Our own sends echo back through both providers.
	if event.get("from_me"):
		return {"status": "ignored", "reason": "outgoing echo"}

	is_group = bool(event.get("is_group"))
	contact = get_or_create(wa_id, event.get("push_name"), is_group)
	record_activity(contact, "Incoming")

	# A voice note becomes an ordinary turn, so everything downstream — the
	# agent, the log, the history — treats it as if they had typed it.
	transcribe_if_voice(settings, event)

	# A photo or PDF has to reach the model as a file. Describing it in words
	# is useless for reading an order off a stock list.
	attach_media(settings, event)

	log_name, first_time = log_incoming(settings, contact, event)

	# Providers send more than one event for a single message: WaClient emits
	# both a chats.update and a messages.upsert. Logging it twice is untidy;
	# answering it twice is worse, and two agents racing on one conversation is
	# what produced the "something went wrong" replies.
	if not first_time:
		return {"status": "ok", "message": log_name, "replied": False, "reason": "already handled"}

	bump_counter(event.get("session"))
	frappe.db.commit()

	skip = should_skip(contact, settings, is_group)
	if skip:
		if skip == "not a known customer":
			notify_unverified(contact, settings, event)
		return {"status": "ok", "message": log_name, "replied": False, "reason": skip}

	# The agent can take several seconds. Acknowledge the message first so the
	# sender sees something happening rather than silence.
	acknowledge(settings, event)

	replied = run_agent(settings, contact, event, log_name)
	frappe.db.commit()

	return {"status": "ok", "message": log_name, "replied": bool(replied)}


def acknowledge(settings, event: dict) -> None:
	"""Blue ticks and a typing indicator, where the provider supports them.

	Best effort throughout: failing to look polite must never stop a reply.
	"""
	if not (settings.mark_messages_read or settings.send_typing_indicator):
		return

	from agent_x.core.transport import get_transport

	try:
		transport = get_transport(session=event.get("session"), settings=settings)
	except Exception:
		return

	chat_id = event.get("chat_id")
	message_id = event.get("message_id")

	if settings.mark_messages_read and chat_id and message_id:
		try:
			transport.mark_read(chat_id, message_id, from_me=False)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "AgentX: could not mark message read")

	if settings.send_typing_indicator:
		try:
			transport.send_presence(chat_id or event.get("wa_id"), "composing")
		except Exception:
			frappe.log_error(frappe.get_traceback(), "AgentX: could not send typing indicator")


def transcribe_if_voice(settings, event: dict) -> None:
	"""Replace a voice note's empty text with what was actually said."""
	from agent_x.agent import audio

	media = event.get("media") or {}
	if not audio.is_voice(event.get("message_type"), media):
		return

	text = audio.transcribe(media, settings)
	if text:
		event["text"] = text
		event["transcribed"] = True
	elif not (event.get("text") or "").strip():
		# Say plainly that it could not be heard, rather than replying to silence.
		event["text"] = ""
		event["transcription_failed"] = True


def attach_media(settings, event: dict) -> None:
	"""Prepare an image or document so the model can actually read it."""
	from agent_x.agent import audio, media

	blob = event.get("media") or {}
	if not blob or audio.is_voice(event.get("message_type"), blob):
		return

	prepared = media.prepare(blob, settings)
	if prepared:
		event["attachment"] = prepared


def should_skip(contact, settings, is_group: bool) -> str | None:
	"""Why this message gets no automated reply, or None to go ahead."""
	if is_group and not settings.reply_to_groups:
		return "group chat"
	if contact.blocked:
		return "contact is blocked"
	if contact.opted_out:
		return "contact opted out"
	if settings.is_excluded(contact.wa_id):
		return "excluded number"
	if not settings.is_allowed(contact.wa_id):
		return "not an allowed number"
	if not settings.ai_enabled:
		return "AI assistant is off"
	if not is_verified_customer(contact, settings):
		return "not a known customer"
	return None


def is_verified_customer(contact, settings) -> bool:
	"""Whether this number belongs to a customer, when that is required."""
	if not settings.only_verified_customers:
		return True

	try:
		from agent_x.agent.tools import customers

		return customers.is_verified(contact)
	except Exception:
		# A lookup that fails must not silently lock everyone out.
		frappe.log_error(frappe.get_traceback(), "AgentX: customer verification failed")
		return True


def notify_unverified(contact, settings, event: dict) -> None:
	"""Tell a stranger why nothing is happening, once.

	Silence reads as a broken number. Repeating it on every message reads as a
	bot arguing, so it is sent only the first time we hear from them.
	"""
	text = (settings.unverified_reply or "").strip()
	if not text:
		return

	# Keyed on how many times they have written in, not on whether a previous
	# notice was delivered. Counting our own sent messages would mean a send
	# that failed is retried on every message they ever send.
	seen = frappe.db.count(
		"WhatsApp Message", {"contact": contact.name, "direction": "Incoming"}
	)
	if seen > 1:
		return

	deliver(text, contact, settings, event.get("session"))


def run_agent(settings, contact, event: dict, log_name: str | None):
	from agent_x.agent import runtime

	if not settings.is_within_business_hours():
		text = (settings.outside_hours_reply or "").strip()
		return deliver(text, contact, settings, event.get("session")) if text else None

	inbound = {
		"message": event.get("text"),
		"message_type": event.get("message_type"),
		"media_filename": (event.get("media") or {}).get("filename"),
		"wa_id": contact.wa_id,
		"transcribed": event.get("transcribed"),
		"transcription_failed": event.get("transcription_failed"),
		"attachment": event.get("attachment"),
	}

	try:
		result = runtime.handle(
			inbound, contact, settings, event.get("session"), exclude_message=log_name
		)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "AgentX: agent failed")
		return None

	if not result or not result.reply:
		return None

	return deliver(result.reply, contact, settings, event.get("session"), run=result.run)


def deliver(text: str, contact, settings, session: str | None, run: str | None = None):
	from agent_x.core.messaging import send_message

	try:
		return send_message(contact.wa_id, text, session=session, agent_run=run, settings=settings)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "AgentX: could not send reply")
		return None


def log_incoming(settings, contact, event: dict) -> tuple[str | None, bool]:
	"""Store the message. Returns its name and whether this is the first sighting.

	The uniqueness of message_id is enforced by the database, not by the check
	below. Two webhooks arriving together can both pass an exists() check, so
	the insert is what actually decides, and a duplicate comes back as an
	integrity error rather than a second row.
	"""
	if not settings.log_messages:
		return None, True

	message_id = event.get("message_id")

	# Cheap path: almost every duplicate is caught here without hitting the
	# integrity error below.
	if message_id:
		existing = frappe.db.get_value(
			"WhatsApp Message", {"message_id": message_id, "direction": "Incoming"}, "name"
		)
		if existing:
			return existing, False

	media = event.get("media") or {}

	doc = frappe.get_doc(
		{
			"doctype": "WhatsApp Message",
			"direction": "Incoming",
			"status": "Received",
			"session": event.get("session"),
			"contact": contact.name,
			"wa_id": event.get("wa_id"),
			"chat_id": event.get("chat_id"),
			"is_group": 1 if event.get("is_group") else 0,
			"message_type": event.get("message_type") or "text",
			"message": event.get("text"),
			"transcribed": 1 if event.get("transcribed") else 0,
			"media_filename": media.get("filename"),
			"media_mimetype": media.get("mimetype"),
			"media_size": media.get("size"),
			"message_id": message_id,
			"sent_on": to_datetime(event.get("timestamp")),
			"raw_payload": frappe.as_json(event.get("raw")) if settings.store_raw_payload else None,
		}
	)
	try:
		doc.insert(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		# Another request logged it first. Frappe raises either depending on
		# whether its own validation or the database index caught it.
		frappe.db.rollback()
		existing = frappe.db.get_value("WhatsApp Message", {"message_id": message_id}, "name")
		return existing, False

	return doc.name, True


def to_datetime(value):
	"""Unix seconds to something Frappe can store; now if it is missing or odd."""
	from datetime import datetime

	try:
		if value:
			return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
	except (ValueError, OSError, OverflowError, TypeError):
		pass

	return now_datetime()


def bump_counter(session: str | None) -> None:
	if not session:
		return

	from agent_x.core.messaging import bump_session

	bump_session(session, "messages_received")
