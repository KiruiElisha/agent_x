"""One linked WhatsApp number.

Pairing is driven from the form: Connect asks the bridge to start a socket, the
bridge posts a `qr` event back, and the webhook pushes it to the open form over
realtime so the QR appears without polling.
"""

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

NAME_PATTERN = re.compile(r"^[a-z0-9._-]{1,64}$")

# The bridge writes credentials to a directory named after the session, so the
# name has to survive being a path segment.
STATE_BY_EVENT = {
	"qr": "Pairing",
	"connected": "Connected",
	"disconnected": "Disconnected",
	"logged_out": "Logged Out",
	"qr_expired": "Disconnected",
}


class WhatsAppSession(Document):
	def validate(self) -> None:
		self.session_name = (self.session_name or "").strip().lower()

		if not NAME_PATTERN.match(self.session_name):
			frappe.throw(
				_(
					"Session Name must be 1 to 64 characters, using only lowercase letters, "
					"digits, dot, dash, or underscore."
				)
			)

		self.stamp_provider()
		self.enforce_single_default()

	def stamp_provider(self) -> None:
		"""Record which provider this session belongs to, and check its needs."""
		settings = frappe.get_cached_doc("AgentX Settings")
		self.provider = settings.whatsapp_provider or "WaClient"

		if self.provider == "WaClient":
			self.instance_id = (self.instance_id or "").strip()
			if not self.instance_id:
				frappe.throw(
					_("A WaClient session needs an Instance ID. Copy it from your WaClient dashboard.")
				)

			clash = frappe.db.get_value(
				"WhatsApp Session",
				{"instance_id": self.instance_id, "name": ("!=", self.name)},
				"name",
			)
			if clash:
				frappe.throw(
					_("Instance {0} is already used by session {1}.").format(
						frappe.bold(self.instance_id), frappe.bold(clash)
					)
				)

	def enforce_single_default(self) -> None:
		if not self.is_default:
			return

		others = frappe.get_all(
			"WhatsApp Session",
			filters={"is_default": 1, "name": ("!=", self.name)},
			pluck="name",
		)
		for other in others:
			frappe.db.set_value("WhatsApp Session", other, "is_default", 0)

	def on_trash(self) -> None:
		"""Drop the credentials on the bridge too, or they linger forever."""
		try:
			self.transport().remove()
		except Exception:
			# A deleted session with an unreachable bridge should still delete.
			frappe.log_error(frappe.get_traceback(), f"AgentX: could not remove session {self.name}")

	def transport(self):
		from agent_x.core.transport import get_transport

		return get_transport(session=self.name)

	# ------------------------------------------------------------------ pairing

	@frappe.whitelist()
	def connect(self) -> dict:
		"""Start the socket. Returns immediately; the QR arrives by realtime."""
		if not self.enabled:
			frappe.throw(_("Enable this session before connecting."))

		transport = self.transport()
		result = transport.start()

		self.apply_status(result)

		# The bridge pushes a QR through the webhook on its own. WaClient does
		# not push anything, so pull one now or the form would sit empty.
		if result.get("state") != "connected":
			try:
				self.fetch_qr()
			except Exception as exc:
				# Not fatal: the operator can press Fetch QR and see the real error.
				frappe.log_error(frappe.get_traceback(), "AgentX: could not fetch QR on connect")
				result["qr_error"] = str(exc)

		return result

	@frappe.whitelist()
	def refresh_status(self) -> dict:
		"""Read the live state from the bridge and store it."""
		result = self.transport().status()
		self.apply_status(result)
		return result

	@frappe.whitelist()
	def fetch_qr(self) -> dict:
		"""Pull the current QR, for when the realtime push was missed."""
		result = self.transport().qr()

		if result.get("qr"):
			self.db_set(
				{
					"qr_data": result["qr"],
					"qr_expires_at": result.get("expires_at"),
					"state": "Pairing",
					"last_error": None,
				},
				notify=True,
				commit=True,
			)
		return result

	@frappe.whitelist()
	def disconnect(self) -> dict:
		"""Close the socket but keep the pairing, so it can resume silently."""
		result = self.transport().stop()
		self.db_set({"state": "Disconnected", "qr_data": None}, notify=True, commit=True)
		return result

	@frappe.whitelist()
	def logout(self) -> dict:
		"""Unlink from the phone. The next connect needs a fresh QR scan."""
		result = self.transport().logout()
		self.db_set(
			{"state": "Logged Out", "qr_data": None, "phone_number": None, "display_name": None},
			notify=True,
			commit=True,
		)
		return result

	@frappe.whitelist()
	def create_instance(self) -> dict:
		"""Mint a WaClient instance and store its id on this session.

		Saves a trip to the WaClient dashboard for the common case of one
		instance per session.
		"""
		settings = frappe.get_cached_doc("AgentX Settings")

		if (settings.whatsapp_provider or "WaClient") != "WaClient":
			frappe.throw(_("Only WaClient sessions have an instance to create."))

		if self.instance_id:
			frappe.throw(
				_("This session already uses instance {0}.").format(frappe.bold(self.instance_id))
			)

		from agent_x.core.transport.waclient import WaClientTransport

		# No instance exists yet, so this call is account level.
		transport = WaClientTransport(self.name, settings, require_instance=False)
		result = transport.create_instance()

		self.db_set("instance_id", result["instance_id"], notify=True, commit=True)
		return result

	@frappe.whitelist()
	def get_pairing_code(self, phone: str) -> dict:
		"""Link by typing a code on the phone, for when scanning is awkward."""
		result = self.transport().pairing_code(phone)

		if result.get("supported"):
			self.db_set({"state": "Pairing", "last_error": None}, notify=True, commit=True)

		return result

	@frappe.whitelist()
	def send_test(self, to: str, message: str) -> dict:
		"""Prove the whole path works, end to end."""
		from agent_x.core.messaging import send_message

		return send_message(to, message, session=self.name)

	# ------------------------------------------------------------------ events

	def apply_status(self, status: dict) -> None:
		"""Store a normalised status block from any provider."""
		mapped = {
			"connected": "Connected",
			"pairing": "Pairing",
			"disconnected": "Disconnected",
			"logged_out": "Logged Out",
		}.get((status.get("state") or "").lower(), "Disconnected")

		values = {
			"state": mapped,
			"last_error": status.get("last_error"),
			"last_event_on": now_datetime(),
		}
		if status.get("phone"):
			values["phone_number"] = status["phone"]
		if mapped == "Connected":
			values["last_connected_on"] = now_datetime()
			values["qr_data"] = None

		self.db_set(values, notify=True, commit=True)


def handle_event(session_name: str, event: str, data: dict) -> None:
	"""Apply one bridge event to the stored session and tell any open form.

	Called from the webhook, so it must never raise: a failure here would make
	the bridge retry a message it already delivered.
	"""
	if not frappe.db.exists("WhatsApp Session", session_name):
		frappe.log_error(
			f"Event {event} for unknown session {session_name}", "AgentX: unknown session"
		)
		return

	doc = frappe.get_doc("WhatsApp Session", session_name)
	values = {"last_event_on": now_datetime()}

	state = STATE_BY_EVENT.get(event)
	if state:
		values["state"] = state

	if event == "qr":
		values["qr_data"] = data.get("qr")
		values["qr_expires_at"] = data.get("expires_at")
		values["last_error"] = None

	elif event == "connected":
		values["qr_data"] = None
		values["qr_expires_at"] = None
		values["last_connected_on"] = now_datetime()
		values["last_error"] = None
		if data.get("phone"):
			values["phone_number"] = data["phone"]
		if data.get("name"):
			values["display_name"] = data["name"]

	elif event in ("disconnected", "logged_out", "qr_expired"):
		values["last_error"] = data.get("reason") or (
			_("The QR code expired before it was scanned.") if event == "qr_expired" else None
		)
		if event == "logged_out":
			values["qr_data"] = None
			values["phone_number"] = None

	doc.db_set(values, notify=True, commit=True)

	# Push straight to whoever has the form open, so the QR appears at once.
	frappe.publish_realtime(
		"agentx_session_update",
		{"session": session_name, "event": event, **values_for_client(values, data)},
		doctype="WhatsApp Session",
		docname=session_name,
	)


def values_for_client(values: dict, data: dict) -> dict:
	return {
		"state": values.get("state"),
		"qr": values.get("qr_data"),
		"qr_expires_at": str(values.get("qr_expires_at") or "") or None,
		"phone": values.get("phone_number"),
		"error": values.get("last_error"),
		"attempt": data.get("attempt"),
	}


def get_default_session() -> str | None:
	name = frappe.db.get_value("WhatsApp Session", {"is_default": 1, "enabled": 1}, "name")
	if name:
		return name

	return frappe.db.get_value("WhatsApp Session", {"enabled": 1, "state": "Connected"}, "name")
