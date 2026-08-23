"""Self-hosted Baileys bridge.

The bridge in `bridge/` owns the WhatsApp socket and renders the QR itself. It
needs somewhere to run a long-lived Node process, so it suits a VPS or your own
server rather than Frappe Cloud.
"""

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


class BridgeTransport(Transport):
	label = "Self-Hosted Bridge"
	supports_qr = True
	# The bridge accepts base64, so nothing has to be exposed publicly.
	needs_public_media = False

	def __init__(self, session: str | None, settings):
		super().__init__(session, settings)

		self.base_url = (settings.bridge_url or "").strip().rstrip("/")
		if not self.base_url:
			frappe.throw(_("Set the Bridge URL in AgentX Settings."))

		self.token = settings.get_password("bridge_api_token", raise_exception=False)
		if not self.token:
			frappe.throw(_("Set the Bridge API Token in AgentX Settings."))

	# ------------------------------------------------------------------ plumbing

	def request(self, method: str, path: str, payload: dict | None = None) -> dict:
		url = f"{self.base_url}/api/{path.lstrip('/')}"
		headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

		try:
			response = requests.request(
				method.upper(),
				url,
				json=payload if method.upper() != "GET" else None,
				params=payload if method.upper() == "GET" else None,
				headers=headers,
				timeout=self.timeout,
			)
		except requests.RequestException as exc:
			raise TransportError(
				_("Could not reach the WhatsApp bridge at {0}: {1}").format(self.base_url, exc)
			) from exc

		return self.parse(response)

	def parse(self, response: requests.Response) -> dict:
		try:
			body = response.json()
		except ValueError:
			raise TransportError(
				_("The bridge returned a non-JSON response ({0}): {1}").format(
					response.status_code, (response.text or "")[:400]
				)
			)

		if response.status_code == 401:
			raise TransportError(_("The bridge rejected the API token. Check AgentX Settings."))

		if response.status_code >= 400 or not body.get("ok", True):
			message = str(body.get("error") or f"HTTP {response.status_code}")
			if "not connected" in message.lower():
				raise NotConnected(
					_("WhatsApp session {0} is not connected. Scan the QR code first.").format(
						self.session_name
					)
				)
			raise TransportError(_("The bridge refused the request: {0}").format(message))

		return body

	# ------------------------------------------------------------------ pairing

	def start(self) -> dict:
		return self.shape(self.request("POST", f"sessions/{self.require_session()}/start"))

	def status(self) -> dict:
		return self.shape(self.request("GET", f"sessions/{self.require_session()}/status"))

	def qr(self) -> dict:
		body = self.request("GET", f"sessions/{self.require_session()}/qr")
		return {
			"session": self.session_name,
			"qr": qr_to_data_url(body.get("qr")),
			"expires_at": body.get("expires_at"),
		}

	def stop(self) -> dict:
		return self.shape(self.request("POST", f"sessions/{self.require_session()}/stop"))

	def logout(self) -> dict:
		return self.shape(self.request("POST", f"sessions/{self.require_session()}/logout"))

	def remove(self) -> dict:
		return self.request("DELETE", f"sessions/{self.require_session()}")

	def shape(self, body: dict) -> dict:
		return {
			"session": self.session_name,
			"state": normalise_state(body.get("state")),
			"phone": body.get("phone"),
			"last_error": body.get("last_error"),
			"raw": body,
		}

	# ---------------------------------------------------------------- messaging

	def send_text(self, to: str, text: str) -> dict:
		body = self.request(
			"POST", f"sessions/{self.require_session()}/send", {"to": to, "text": text}
		)
		return {"message_id": body.get("message_id"), "raw": body}

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
		media = {
			"url": url,
			"base64": base64_content,
			"kind": kind,
			"mimetype": mimetype,
			"filename": filename,
			"caption": caption,
		}
		body = self.request(
			"POST",
			f"sessions/{self.require_session()}/send",
			{"to": to, "media": {k: v for k, v in media.items() if v is not None}},
		)
		return {"message_id": body.get("message_id"), "raw": body}

	def check_number(self, number: str) -> dict:
		body = self.request(
			"POST", f"sessions/{self.require_session()}/check", {"number": number}
		)
		return {"exists": bool(body.get("exists")), "checked": True}

	def list_sessions(self) -> list[dict]:
		return self.request("GET", "sessions").get("sessions") or []

	def register_webhook(self, url: str) -> dict:
		# The bridge is told its webhook through its own environment, because it
		# has to know the URL before Frappe ever calls it.
		return {
			"supported": False,
			"note": _("Set BRIDGE_WEBHOOK_URL in the bridge environment and restart it."),
		}
