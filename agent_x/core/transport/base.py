"""What every WhatsApp provider must be able to do.

The agent, the policy gate, and the audit trail never learn which provider is in
use. Swapping WaClient for a self-hosted bridge is a settings change, not a code
change.
"""

import base64
import io

import frappe
from frappe import _


class TransportError(frappe.ValidationError):
	"""The provider could not be reached, or refused the request."""


class NotConnected(TransportError):
	"""The session exists but no phone is linked, so nothing can be sent."""


class Transport:
	"""One WhatsApp session, whoever is hosting it."""

	#: Shown in the desk so the operator knows what they are looking at.
	label = "Transport"

	#: False when pairing happens somewhere else and Desk cannot show a QR.
	supports_qr = True

	#: True when media must be fetched from a URL the provider can reach, which
	#: means a generated PDF has to be public for a while. The self-hosted
	#: bridge takes bytes directly and sets this False.
	needs_public_media = True

	def __init__(self, session: str | None, settings):
		self.settings = settings
		self.session_name = session
		self.timeout = settings.request_timeout or 30

	def require_session(self) -> str:
		if not self.session_name:
			frappe.throw(_("No WhatsApp session was given and no default is set."))
		return self.session_name

	# ------------------------------------------------------------------ pairing

	def start(self) -> dict:
		"""Begin or resume a session. Returns a status dict."""
		raise NotImplementedError

	def status(self) -> dict:
		"""Current state. Must return at least {"state": ...}.

		`state` is one of: disconnected, pairing, connected, logged_out.
		"""
		raise NotImplementedError

	def qr(self) -> dict:
		"""Current pairing QR: {"qr": <png data url or None>, "expires_at": ...}."""
		raise NotImplementedError

	def stop(self) -> dict:
		"""Close the connection but keep the pairing."""
		raise NotImplementedError

	def logout(self) -> dict:
		"""Unlink the phone and forget the credentials."""
		raise NotImplementedError

	def remove(self) -> dict:
		"""Delete the session entirely."""
		return self.logout()

	# ---------------------------------------------------------------- messaging

	def send_text(self, to: str, text: str) -> dict:
		"""Returns {"message_id": ...}."""
		raise NotImplementedError

	def send_media(self, to: str, **kwargs) -> dict:
		raise NotImplementedError

	def check_number(self, number: str) -> dict:
		"""Whether a number is reachable on WhatsApp."""
		raise NotImplementedError

	# ------------------------------------------------------------------ webhook

	def register_webhook(self, url: str) -> dict:
		"""Point the provider at our webhook. Not every provider needs this."""
		return {"supported": False}

	# ------------------------------------------------------- optional extras
	#
	# Everything below is a convenience some providers offer and others do not.
	# The default says "not supported" rather than raising, so a caller can ask
	# for a typing indicator without first checking who the provider is.

	def send_link(self, to: str, text: str, url: str) -> dict:
		"""Text with a URL preview card."""
		return self.send_text(to, f"{text}\n{url}".strip())

	def send_location(
		self, to: str, latitude: float, longitude: float, name: str | None = None,
		address: str | None = None, live: bool = False,
	) -> dict:
		return {"supported": False}

	def send_poll(self, to: str, question: str, options: list, multiple: bool = False) -> dict:
		return {"supported": False}

	def mark_read(self, chat_id: str, message_id: str, from_me: bool = False) -> dict:
		"""Blue ticks. Worth doing before a slow reply so the sender knows it landed."""
		return {"supported": False}

	def send_presence(self, to: str, presence: str = "composing") -> dict:
		"""Typing or recording indicator."""
		return {"supported": False}

	def react(self, chat_id: str, message_id: str, emoji: str, from_me: bool = False) -> dict:
		return {"supported": False}

	def delete_message(self, chat_id: str, message_id: str, from_me: bool = True) -> dict:
		return {"supported": False}

	def forward_message(self, to: str, chat_id: str, message_id: str) -> dict:
		return {"supported": False}

	def get_chats(self, limit: int = 50) -> dict:
		return {"supported": False}

	def get_groups(self) -> dict:
		return {"supported": False}

	def get_messages(self, chat_id: str, limit: int = 50) -> dict:
		return {"supported": False}

	def pairing_code(self, phone: str) -> dict:
		"""Link by typing a code on the phone, instead of scanning."""
		return {"supported": False}


# ---------------------------------------------------------------------- shared


def qr_to_data_url(value: str | None) -> str | None:
	"""Turn whatever the provider calls a QR into an <img>-ready data URL.

	Providers return one of three things: a data URL already, a bare base64
	image, or the raw pairing string that still has to be drawn.
	"""
	if not value:
		return None

	text = str(value).strip()

	if text.startswith("data:image"):
		return text

	# A bare base64 image. PNG and JPEG have recognisable prefixes once encoded.
	if text.startswith(("iVBORw0KGgo", "/9j/")):
		mime = "image/png" if text.startswith("iVBORw0KGgo") else "image/jpeg"
		return f"data:{mime};base64,{text}"

	# Otherwise it is the pairing payload itself, so draw it.
	return render_qr(text)


def render_qr(payload: str) -> str | None:
	"""Draw a QR payload as a PNG data URL."""
	try:
		import qrcode
	except ImportError:
		frappe.log_error(
			"The qrcode package is not installed, so a raw QR string cannot be drawn.",
			"AgentX: cannot render QR",
		)
		return None

	try:
		image = qrcode.make(payload)
		buffer = io.BytesIO()
		image.save(buffer, format="PNG")
		encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
		return f"data:image/png;base64,{encoded}"
	except Exception:
		frappe.log_error(frappe.get_traceback(), "AgentX: QR rendering failed")
		return None


def normalise_state(value: str | None) -> str:
	"""Map a provider's connection word onto our four states."""
	text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")

	if text in ("connected", "open", "authenticated", "online", "ready", "active"):
		return "connected"
	# WaClient documents: pending, linking, connecting, connected,
	# disconnected, logged_out. Everything before "connected" is pairing.
	if text in (
		"pairing", "connecting", "qr", "scan_qr", "waiting_for_qr", "got_qr",
		"pending", "linking",
	):
		return "pairing"
	if text in ("logged_out", "loggedout", "unpaired", "disconnected_logged_out", "removed"):
		return "logged_out"

	return "disconnected"
