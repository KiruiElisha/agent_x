"""Flattens WaClient webhook payloads into the shape the rest of AgentX expects.

WaClient wraps Baileys output and the nesting has changed between versions, so
every lookup falls back through the shapes seen in the wild rather than assuming
one layout. The self-hosted bridge already sends the flat shape, so it skips all
of this.

Adapted from the parser proven in the earlier `whatsapp` app.
"""

from agent_x.core.phone import from_jid

MEDIA_KEYS = (
	"imageMessage",
	"videoMessage",
	"documentMessage",
	"audioMessage",
	"stickerMessage",
	"documentWithCaptionMessage",
)

# Containers whose text lives under a differently named key.
TEXT_KEYS = (
	("conversation", None),
	("extendedTextMessage", "text"),
	("buttonsResponseMessage", "selectedDisplayText"),
	("templateButtonReplyMessage", "selectedDisplayText"),
	("listResponseMessage", "title"),
)

# WaClient's ack numbers, same scale Baileys uses.
ACK_STATUS = {
	"1": "Sent",
	"2": "Sent",
	"3": "Delivered",
	"4": "Read",
	"5": "Read",
	"sent": "Sent",
	"server_ack": "Sent",
	"delivered": "Delivered",
	"delivery_ack": "Delivered",
	"read": "Read",
	"played": "Read",
	"failed": "Failed",
	"error": "Failed",
}


def as_dict(value) -> dict:
	return value if isinstance(value, dict) else {}


def first_entry(value):
	"""Webhooks send either one message object or a list of them."""
	if isinstance(value, list):
		return value[0] if value else {}
	return value


def is_group_id(chat_id: str | None) -> bool:
	return str(chat_id or "").endswith("@g.us")


# Containers WaClient nests the Baileys envelope inside, in the order worth
# trying. It has shipped several shapes, and a `chats.update` carrying a new
# message looks nothing like a plain `messages.upsert`.
ENVELOPE_KEYS = ("data", "messages", "message", "body_message", "chats", "update", "message_payload")

MAX_DEPTH = 8


def find_envelope(node, depth: int = 0):
	"""The first Baileys envelope anywhere in this payload.

	Searching by shape rather than by path, because the path differs per event
	type and per WaClient version. An envelope is recognisable: a dict with a
	`key` holding a `remoteJid`.
	"""
	if depth > MAX_DEPTH or node is None:
		return None

	if isinstance(node, list):
		for item in node[:10]:
			found = find_envelope(item, depth + 1)
			if found:
				return found
		return None

	if not isinstance(node, dict):
		return None

	key = node.get("key")
	if isinstance(key, dict) and key.get("remoteJid"):
		return node

	# Named containers first, so the common shapes cost the least.
	for name in ENVELOPE_KEYS:
		if name in node:
			found = find_envelope(node[name], depth + 1)
			if found:
				return found

	for value in node.values():
		if isinstance(value, (dict, list)):
			found = find_envelope(value, depth + 1)
			if found:
				return found

	return None


def sender_jid(key: dict, fallback: str = "") -> tuple[str, str]:
	"""Who sent it and which chat it belongs to.

	WhatsApp's newer addressing puts a `@lid` in remoteJid, which is an opaque
	per-chat identifier rather than a phone number. The real number arrives
	alongside it in remoteJidAlt, and using the lid instead would mean the
	sender never matches an allowed number.
	"""
	remote = str(key.get("remoteJid") or "")
	alt = str(key.get("remoteJidAlt") or "")

	# A group addresses the group in remoteJid and the person in participant.
	if remote.endswith("@g.us"):
		person = str(key.get("participantAlt") or key.get("participant") or "")
		if person.endswith("@lid"):
			person = str(key.get("participantAlt") or "") or person
		return remote, person or remote

	if remote.endswith("@lid"):
		chat = alt or fallback or remote
		return chat, chat

	return (remote or alt or fallback), (remote or alt or fallback)


def chat_hint(payload: dict) -> str:
	"""A real jid from the chat wrapper, when the envelope only has a lid."""
	body = as_dict(payload.get("data")) or payload
	entries = body.get("data")

	if isinstance(entries, list):
		for entry in entries[:5]:
			if isinstance(entry, dict) and isinstance(entry.get("id"), str):
				return entry["id"]

	for name in ("id", "chat_id", "from_contact", "from"):
		value = body.get(name)
		if isinstance(value, str) and "@" in value:
			return value

	return ""


def parse(payload: dict) -> dict | None:
	"""Return a normalised message dict, or None if there is no message in here."""
	payload = as_dict(payload)
	body = as_dict(payload.get("data")) or payload

	event = body.get("event") or payload.get("event") or ""
	instance_id = payload.get("instance_id") or body.get("instance_id")

	envelope = find_envelope(payload)
	if not envelope:
		return None

	key = as_dict(envelope.get("key"))
	content = unwrap(as_dict(envelope.get("message")))

	chat_id, from_jid_value = sender_jid(key, chat_hint(payload))
	if not chat_id:
		return None

	text, message_type, media = extract_content(content)

	# An event with an envelope but nothing readable is an ack or a presence
	# update, not something to answer.
	if not (text or media or is_ack_event(event)):
		return None

	return {
		"event": event,
		"instance_id": instance_id,
		"chat_id": chat_id,
		"wa_id": from_jid(from_jid_value),
		"is_group": is_group_id(chat_id),
		"from_me": bool(key.get("fromMe")),
		"message_id": key.get("id") or envelope.get("id"),
		"push_name": envelope.get("pushName") or envelope.get("verifiedBizName"),
		"text": text,
		"message_type": message_type,
		"media": media or None,
		"timestamp": extract_timestamp(envelope, content),
	}


def is_ack_event(event: str) -> bool:
	"""Only a genuine receipt may arrive without readable content.

	Deliberately narrower than it looks: "chats.update" contains "update" but
	carries real messages, and letting it through empty would have the assistant
	answering silence.
	"""
	name = str(event or "").lower()
	return "ack" in name or "receipt" in name or "status" in name


def unwrap(content: dict) -> dict:
	"""Strip the layers WhatsApp wraps a real message in."""
	for _ in range(4):
		inner = None
		for wrapper in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2",
		                "viewOnceMessageV2Extension", "documentWithCaptionMessage", "editedMessage"):
			nested = as_dict(content.get(wrapper))
			if nested:
				inner = as_dict(nested.get("message")) or nested
				break
		if not inner:
			break
		content = inner

	return content


def extract_content(content: dict) -> tuple[str, str, dict]:
	"""Return the readable text, a message type label, and any media details."""
	if not content:
		return "", "text", {}

	for key in MEDIA_KEYS:
		media = as_dict(content.get(key))
		if media:
			return (
				media.get("caption") or "",
				key.replace("Message", "").lower(),
				{
					"url": media.get("url") or media.get("directPath"),
					"filename": media.get("fileName") or media.get("filename"),
					"mimetype": media.get("mimetype"),
					"size": to_int(media.get("fileLength")),
				},
			)

	location = as_dict(content.get("locationMessage"))
	if location:
		lat, lng = location.get("degreesLatitude"), location.get("degreesLongitude")
		return f"{lat}, {lng}", "location", {}

	contact = as_dict(content.get("contactMessage"))
	if contact:
		return contact.get("displayName") or "", "contact", {}

	for key, subkey in TEXT_KEYS:
		value = content.get(key)
		if value is None:
			continue
		if subkey is None and isinstance(value, str):
			return value, "text", {}
		nested = as_dict(value)
		if nested.get(subkey):
			return nested[subkey], "text", {}

	# Unknown container: report its name so the log still says something useful.
	label = next((k for k in content if k != "messageContextInfo"), "unknown")
	return "", label.replace("Message", "").lower(), {}


def to_int(value):
	try:
		return int(value)
	except (TypeError, ValueError):
		return None


def extract_timestamp(envelope: dict, content: dict) -> int | None:
	raw = (
		envelope.get("messageTimestamp")
		or envelope.get("t")
		or as_dict(as_dict(content.get("messageContextInfo")).get("deviceListMetadata")).get(
			"senderTimestamp"
		)
	)

	if isinstance(raw, dict):
		raw = raw.get("low") or raw.get("value")

	seconds = to_int(raw)
	if not seconds or seconds <= 0:
		return None

	# Some builds send milliseconds.
	if seconds > 10_000_000_000:
		seconds //= 1000

	return seconds


def is_status_event(parsed: dict) -> bool:
	event = str(parsed.get("event") or "").lower()
	return "ack" in event or "status" in event


def extract_ack(payload: dict, parsed: dict) -> str | None:
	"""Map an ack event onto a WhatsApp Message status."""
	data = as_dict(payload.get("data"))
	raw = (
		data.get("status")
		or data.get("ack")
		or payload.get("status")
		or payload.get("ack")
	)
	return ACK_STATUS.get(str(raw).lower().strip()) if raw is not None else None
