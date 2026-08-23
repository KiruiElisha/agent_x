"""WaClient: a hosted WhatsApp Web gateway.

WaClient keeps the session on its own servers and exposes it over HTTP, so
nothing long-lived has to run next to Frappe. That is what makes it the provider
to use on Frappe Cloud, where there is nowhere to put a persistent WebSocket.
The trade is that a third party holds the session and can read the messages.

Endpoints and payload shapes here follow https://waclient.com/docs/whatsapp-web-api.
Everything lives on one host and takes JSON; `instance_id` and `access_token`
are added to every request.
"""

import json

import requests

import frappe
from frappe import _

from agent_x.core.transport.base import (
	NotConnected,
	Transport,
	TransportError,
	normalise_state,
	qr_to_data_url,
)

DEFAULT_BASE = "https://api.waclient.com"

# Presence values the API accepts.
PRESENCE = ("composing", "recording", "paused")


class WaClientTransport(Transport):
	label = "WaClient"
	supports_qr = True

	def __init__(self, session: str | None, settings, require_instance: bool = True):
		super().__init__(session, settings)

		self.base_url = (settings.waclient_api_url or DEFAULT_BASE).rstrip("/")

		self.access_token = settings.get_password("waclient_access_token", raise_exception=False)
		if not self.access_token:
			frappe.throw(_("Set the WaClient Access Token in AgentX Settings."))

		# Account-level calls (create an instance, list them) have no instance yet.
		self.instance_id = self.resolve_instance() if require_instance else None

	def resolve_instance(self) -> str:
		if not self.session_name:
			frappe.throw(
				_(
					"No WhatsApp session is set. Create a WhatsApp Session with your WaClient "
					"Instance ID, or set a Default Session in AgentX Settings."
				)
			)

		instance_id = frappe.db.get_value("WhatsApp Session", self.session_name, "instance_id")
		if not instance_id:
			frappe.throw(
				_("WhatsApp Session {0} has no WaClient Instance ID. Add it before connecting.").format(
					self.session_name
				)
			)
		return instance_id

	# ------------------------------------------------------------------ plumbing

	def credentials(self) -> dict:
		creds = {"access_token": self.access_token}
		if self.instance_id:
			creds["instance_id"] = self.instance_id
		return creds

	def call(
		self,
		path: str,
		payload: dict | None = None,
		method: str = "POST",
		as_form: bool = False,
	) -> dict:
		"""One request. Everything documented takes JSON; `as_form` is a fallback."""
		url = f"{self.base_url}/{path.lstrip('/')}"
		data = {**(payload or {}), **self.credentials()}
		headers = {"Accept": "application/json", "User-Agent": "Frappe-AgentX/1.0"}

		verb = method.upper()

		try:
			if verb == "GET":
				response = requests.get(url, params=data, headers=headers, timeout=self.timeout)
			elif as_form:
				response = requests.request(
					verb, url, data=data, headers=headers, timeout=self.timeout
				)
			else:
				response = requests.request(
					verb, url, json=data, headers=headers, timeout=self.timeout
				)
		except requests.RequestException as exc:
			raise TransportError(_("Could not reach WaClient: {0}").format(exc)) from exc

		return self.parse(response)

	def parse(self, response: requests.Response) -> dict:
		try:
			body = response.json()
		except ValueError:
			raise TransportError(
				_("WaClient returned a non-JSON response ({0}): {1}").format(
					response.status_code, (response.text or "")[:400]
				)
			)

		if response.status_code >= 400:
			raise TransportError(
				_("WaClient error {0}: {1}").format(
					response.status_code, body.get("message") or json.dumps(body)[:400]
				)
			)

		# WaClient answers 200 with {"status": "error", ...} on business failures.
		if str(body.get("status", "")).lower() in ("error", "false", "fail", "failed"):
			message = str(body.get("message") or json.dumps(body)[:400])
			lowered = message.lower()
			if "not connected" in lowered or "disconnect" in lowered or "logged out" in lowered:
				raise NotConnected(
					_("WhatsApp session {0} is not connected. Scan the QR code first.").format(
						self.session_name
					)
				)
			raise TransportError(_("WaClient rejected the request: {0}").format(message))

		return body

	@staticmethod
	def target(to: str) -> dict:
		"""WaClient takes either a bare number or a full JID under chat_id."""
		value = str(to or "").strip()
		if "@" in value:
			return {"chat_id": value}
		return {"number": "".join(c for c in value if c.isdigit())}

	# ------------------------------------------------------------------ pairing

	def start(self) -> dict:
		"""Bring the instance up and report where it got to."""
		current = self.status()

		if current.get("state") == "connected":
			return current

		# A logged out instance needs a fresh login rather than a reconnect.
		try:
			if current.get("state") == "logged_out":
				self.call("relogin_qrcode", method="POST")
			else:
				self.call("reconnect", method="POST")
		except TransportError:
			# Neither applies to a brand new instance, which goes straight to a QR.
			pass

		return self.status()

	def status(self) -> dict:
		body = self.call("instance_status", {"live": 1}, method="GET")
		data = body.get("data") or body

		state = normalise_state(data.get("connection_state") or data.get("status"))
		if data.get("relogin_required"):
			state = "logged_out"

		phone = None
		try:
			phone = ((self.info() or {}).get("account") or {}).get("phone")
		except TransportError:
			# Status is what we needed; the extra detail is a bonus.
			pass

		return {
			"session": self.session_name,
			"state": state,
			"phone": phone,
			"relogin_required": bool(data.get("relogin_required")),
			"raw": data,
		}

	def info(self) -> dict:
		return self.call("instance_info", method="GET").get("data") or {}

	def qr(self) -> dict:
		"""The pairing QR, as a data URL Desk can render.

		`get_qrcode` covers an instance that has never been linked. One that was
		logged out needs `relogin_qrcode` to mint a new code.
		"""
		body = self.call("get_qrcode", method="GET")
		image = extract_qr(body)

		if not image:
			body = self.call("relogin_qrcode", method="POST")
			image = extract_qr(body)

		if not image:
			raise TransportError(
				_("WaClient did not return a QR code: {0}").format(
					str(body.get("message") or json.dumps(body))[:300]
				)
			)

		# WaClient does not say how long the code lasts; WhatsApp rotates ~60s.
		return {"session": self.session_name, "qr": image, "expires_at": None}

	def pairing_code(self, phone: str) -> dict:
		"""An 8 character code typed on the phone, instead of scanning."""
		digits = "".join(c for c in str(phone or "") if c.isdigit())
		if not digits:
			frappe.throw(_("A phone number in international format is needed for a pairing code."))

		body = self.call("get_paircode", {"phone": digits}, method="GET")
		code = body.get("paircode") or (body.get("data") or {}).get("paircode")

		if not code:
			body = self.call("relogin_paircode", {"phone": digits}, method="POST")
			code = body.get("paircode") or (body.get("data") or {}).get("paircode")

		if not code:
			raise TransportError(_("WaClient did not return a pairing code."))

		return {"supported": True, "pairing_code": code, "phone": digits}

	def stop(self) -> dict:
		"""WaClient has no "close but keep the pairing", so say so plainly."""
		return {
			"session": self.session_name,
			"state": "connected",
			"supported": False,
			"note": _("WaClient has no disconnect that keeps the pairing. Use Log Out instead."),
		}

	def logout(self) -> dict:
		self.call("logout", method="POST")
		return {"session": self.session_name, "state": "logged_out"}

	def remove(self) -> dict:
		"""Unlink, but leave the instance on the WaClient account."""
		return self.logout()

	def delete_instance(self) -> dict:
		"""Destroy the instance at WaClient. Not reversible."""
		self.call("delete_instance", method="POST")
		return {"session": self.session_name, "deleted": True}

	# ---------------------------------------------------------------- account

	def create_instance(self) -> dict:
		"""Mint a new instance on the account. Needs no existing instance."""
		body = self.call("create_instance", method="POST")
		instance_id = body.get("instance_id") or (body.get("data") or {}).get("instance_id")

		if not instance_id:
			raise TransportError(_("WaClient did not return a new instance ID."))

		return {"instance_id": instance_id}

	def list_instances(self) -> list:
		body = self.call("list_instances", method="POST")
		data = body.get("data") or body.get("instances") or []
		return data if isinstance(data, list) else data.get("instances") or []

	# ---------------------------------------------------------------- messaging

	def send_text(self, to: str, text: str) -> dict:
		body = self.call("send", {**self.target(to), "type": "text", "message": text})
		return {"message_id": extract_message_id(body), "raw": body}

	def send_link(self, to: str, text: str, url: str) -> dict:
		"""Text with a link preview card."""
		body = self.call(
			"send", {**self.target(to), "type": "link", "message": text, "url": url}
		)
		return {"message_id": extract_message_id(body), "raw": body}

	def send_media(
		self,
		to: str,
		*,
		url: str | None = None,
		base64_content: str | None = None,
		kind: str = "document",
		mimetype: str | None = None,
		filename: str | None = None,
		caption: str | None = None,
	) -> dict:
		if not url:
			frappe.throw(
				_("WaClient sends media from a public URL. Attach the file and pass its URL.")
			)

		# type "media" lets WaClient detect the kind from the URL extension.
		# A filename forces document mode, which is what "document" should mean.
		payload = {**self.target(to), "type": "media", "message": caption or "", "media_url": url}

		if filename or kind == "document":
			payload["filename"] = filename or url.rsplit("/", 1)[-1] or "file"

		body = self.call("send", payload)
		return {"message_id": extract_message_id(body), "raw": body}

	def send_location(
		self,
		to: str,
		latitude: float,
		longitude: float,
		name: str | None = None,
		address: str | None = None,
		live: bool = False,
	) -> dict:
		message = {"latitude": latitude, "longitude": longitude}
		if name:
			message["name"] = name
		if address:
			message["address"] = address
		if live:
			message["live"] = True

		body = self.call(
			"send",
			{**self.target(to), "type": "live_location" if live else "location", "message": message},
		)
		return {"message_id": extract_message_id(body), "raw": body}

	def send_poll(self, to: str, question: str, options: list, multiple: bool = False) -> dict:
		choices = [str(o).strip() for o in (options or []) if str(o).strip()]
		if len(choices) < 2:
			frappe.throw(_("A poll needs at least two options."))

		body = self.call(
			"send",
			{
				**self.target(to),
				"type": "poll",
				"message": {
					"name": question,
					"options": choices,
					"selectableCount": len(choices) if multiple else 1,
				},
			},
		)
		return {"message_id": extract_message_id(body), "raw": body}

	def forward_message(self, to: str, chat_id: str, message_id: str) -> dict:
		body = self.call(
			"forward_message",
			{**self.target(to), "from_chat_id": chat_id, "message_id": message_id},
			method="POST",
		)
		return {"message_id": extract_message_id(body), "raw": body}

	def delete_message(self, chat_id: str, message_id: str, from_me: bool = True) -> dict:
		body = self.call(
			"delete_message",
			{"chat_id": chat_id, "message_id": message_id, "from_me": bool(from_me)},
			method="POST",
		)
		return {"supported": True, "raw": body}

	def react(self, chat_id: str, message_id: str, emoji: str, from_me: bool = False) -> dict:
		body = self.call(
			"react_to_message",
			{"chat_id": chat_id, "message_id": message_id, "emoji": emoji, "from_me": bool(from_me)},
			method="PUT",
		)
		return {"supported": True, "raw": body}

	def mark_read(self, chat_id: str, message_id: str, from_me: bool = False) -> dict:
		body = self.call(
			"mark_message_read",
			{"chat_id": chat_id, "message_id": message_id, "from_me": bool(from_me)},
			method="PUT",
		)
		return {"supported": True, "raw": body}

	def send_presence(self, to: str, presence: str = "composing") -> dict:
		if presence not in PRESENCE:
			frappe.throw(_("Presence must be one of: {0}").format(", ".join(PRESENCE)))

		body = self.call(
			"send_chat_presence", {**self.target(to), "presence": presence}, method="PUT"
		)
		return {"supported": True, "raw": body}

	# ------------------------------------------------------- reading the account

	def check_number(self, number: str) -> dict:
		result = self.check_numbers([number])
		first = (result.get("results") or [{}])[0]
		return {"exists": first.get("exists"), "jid": first.get("jid"), "checked": True}

	def check_numbers(self, numbers: list) -> dict:
		"""Validate several numbers in one call."""
		cleaned = ["".join(c for c in str(n) if c.isdigit()) for n in numbers or []]
		cleaned = [n for n in cleaned if n]
		if not cleaned:
			frappe.throw(_("Give at least one number to check."))

		body = self.call("check_number", {"numbers": cleaned}, method="POST")
		data = body.get("data") or {}
		return {"results": data.get("results") or [], "raw": body}

	def get_chats(self, limit: int = 50) -> dict:
		body = self.call("get_chats", {"limit": limit}, method="POST")
		return {"supported": True, "chats": body.get("data") or []}

	def get_groups(self) -> dict:
		body = self.call("get_groups", method="GET")
		return {"supported": True, "groups": body.get("data") or []}

	def get_messages(self, chat_id: str, limit: int = 50) -> dict:
		body = self.call(
			"get_messages_by_chat", {"chat_id": chat_id, "limit": limit}, method="GET"
		)
		data = body.get("data") or {}
		return {"supported": True, "messages": data.get("messages") or [], "count": data.get("count")}

	# ------------------------------------------------------------------ webhook

	def register_webhook(self, url: str) -> dict:
		payload = {"webhook_url": url, "enable": True}

		try:
			body = self.call("set_webhook", payload, method="POST")
		except TransportError:
			# Older WaClient builds only read form-encoded fields here and want
			# the literal string "true". Try that before giving up.
			body = self.call(
				"set_webhook", {**payload, "enable": "true"}, method="POST", as_form=True
			)

		registered = {}
		try:
			registered = self.get_webhook()
		except TransportError:
			pass

		return {
			"supported": True,
			"webhook_url": url,
			"response": body,
			"registered_url": registered.get("webhook_url"),
			"verified": registered.get("webhook_url") == url and bool(registered.get("enabled")),
		}

	def get_webhook(self) -> dict:
		body = self.call("get_webhook", method="POST")
		data = body.get("data") or body
		return data.get("webhook") or data


# ---------------------------------------------------------------------- helpers


def extract_qr(body: dict) -> str | None:
	"""Find the QR in whatever shape WaClient wrapped it in.

	Documented today as a top level `base64` holding a full data URL, but it has
	appeared under `data` and other names, so check around.
	"""
	data = body.get("data") if isinstance(body.get("data"), dict) else {}

	for source in (body, data):
		for key in ("base64", "qrcode", "qr_code", "qr", "image", "qr_image"):
			value = source.get(key)
			if isinstance(value, str) and value.strip():
				rendered = qr_to_data_url(value)
				if rendered:
					return rendered

	return None


def extract_message_id(body: dict) -> str | None:
	"""Pull the message key out of a send response.

	Documented as `message_payload.key.id`. Older builds used `data` or
	`message`, so all three are checked.
	"""
	for key in ("message_payload", "data", "message"):
		block = body.get(key)
		if isinstance(block, dict):
			message_id = (block.get("key") or {}).get("id")
			if message_id:
				return message_id

	return None
