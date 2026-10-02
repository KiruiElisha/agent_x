"""Central configuration for AgentX.

One Single doctype holds the bridge credentials, the AI provider, and the
policy that decides what the assistant may do to documents.
"""

import datetime

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, get_time, get_url, now_datetime

WEBHOOK_PATH = "/api/method/agent_x.core.webhook.receive"

DAY_ALIASES = {
	"mon": 0, "monday": 0,
	"tue": 1, "tues": 1, "tuesday": 1,
	"wed": 2, "weds": 2, "wednesday": 2,
	"thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
	"fri": 4, "friday": 4,
	"sat": 5, "saturday": 5,
	"sun": 6, "sunday": 6,
}

# Nothing the assistant does should ever touch these, whatever the policy says.
# They are the doctypes that grant access or hold secrets.
FORBIDDEN_DOCTYPES = {
	"User", "Role", "Role Profile", "Custom Role", "User Permission",
	"DocPerm", "Custom DocPerm", "DocType", "Custom Field", "Property Setter",
	"Server Script", "Client Script", "Scheduled Job Type", "System Settings",
	"AgentX Settings", "Webhook", "Social Login Key", "OAuth Client",
	"LDAP Settings", "API Key", "Access Log", "Personal Data Deletion Request",
}


class AgentXSettings(Document):
	def validate(self) -> None:
		self.bridge_url = (self.bridge_url or "").strip().rstrip("/")
		self.public_base_url = (self.public_base_url or "").strip().rstrip("/")
		self.webhook_url = self.build_webhook_url()
		base = (self.public_base_url or "").rstrip("/") or get_url()
		self.intasend_webhook_url = f"{base}/api/method/agent_x.billing.intasend"

		self.validate_instance()
		self.ensure_webhook_token()
		self.validate_policies()
		self.validate_numbers()
		self.validate_working_days()
		self.validate_automation()

	def build_webhook_url(self) -> str:
		"""Where the bridge should post events.

		get_url() reflects the current request, so it returns a loopback address
		when this is saved from the console. The override exists for that case.
		"""
		if self.public_base_url:
			return f"{self.public_base_url}{WEBHOOK_PATH}"
		return get_url(WEBHOOK_PATH)

	def validate_instance(self) -> None:
		self.waclient_instance_id = (self.waclient_instance_id or "").strip()

	def ensure_webhook_token(self) -> None:
		"""The inbound endpoint refuses everything without a token, so never lack one.

		A new token only works once the provider has it, so say so.
		"""
		if self.webhook_token:
			return

		self.webhook_token = frappe.generate_hash(length=40)
		if (self.whatsapp_provider or "WaClient") == "WaClient":
			frappe.msgprint(
				_("A Webhook Token was generated. Press Register Webhook so WaClient sends it."),
				title=_("Webhook Token"),
				indicator="orange",
			)

	def on_update(self) -> None:
		self.sync_session()
		self.sync_billing()
		self.sync_clients()

	def sync_clients(self) -> None:
		from agent_x.billing import pull_clients, remember_site

		remember_site(self)
		pull_clients(self)

	def sync_billing(self) -> None:
		from agent_x.billing import push_to_bridge

		if self.subscriptions_enabled or self.intasend_secret_key:
			push_to_bridge(self)

	def validate_policies(self) -> None:
		seen: set[str] = set()

		for row in self.doctype_policies:
			if row.document_type in FORBIDDEN_DOCTYPES:
				frappe.throw(
					_("Row {0}: {1} can never be automated. It controls access or holds secrets.").format(
						row.idx, frappe.bold(row.document_type)
					)
				)

			if row.document_type in seen:
				frappe.throw(
					_("Row {0}: there is already a policy for {1}.").format(
						row.idx, frappe.bold(row.document_type)
					)
				)
			seen.add(row.document_type)

			# Writing without reading is not a coherent permission set: the
			# assistant has to find a document before it can change one.
			if (row.can_write or row.can_submit or row.can_cancel or row.can_delete) and not row.can_read:
				row.can_read = 1

			meta = frappe.get_meta(row.document_type)
			if (row.can_submit or row.can_cancel) and not meta.is_submittable:
				frappe.throw(
					_("Row {0}: {1} is not submittable, so submit and cancel do not apply.").format(
						row.idx, frappe.bold(row.document_type)
					)
				)

	def validate_numbers(self) -> None:
		from agent_x.core.phone import digits_only

		for row in self.allowed_numbers:
			cleaned = digits_only(row.phone_number)
			if not cleaned:
				frappe.throw(
					_("Allowed Numbers row {0}: {1} is not a usable phone number.").format(
						row.idx, row.phone_number
					)
				)
			row.phone_number = cleaned

		for row in self.excluded_numbers:
			cleaned = digits_only(row.phone_number)
			if not cleaned:
				frappe.throw(
					_("Excluded Numbers row {0}: {1} is not a usable phone number.").format(
						row.idx, row.phone_number
					)
				)
			row.phone_number = cleaned

		if self.reply_scope == "Only Allowed Numbers" and not self.allowed_numbers:
			frappe.msgprint(
				_("Reply To is Only Allowed Numbers but the list is empty, so nobody will get a reply."),
				title=_("No Allowed Numbers"),
				indicator="orange",
			)

	def validate_working_days(self) -> None:
		if not self.restrict_business_hours:
			return

		unknown = [
			token for token in split_days(self.working_days) if token.casefold() not in DAY_ALIASES
		]
		if unknown:
			frappe.throw(
				_("Unrecognised working days: {0}. Use short names like Mon,Tue,Wed.").format(
					", ".join(unknown)
				)
			)

	def validate_automation(self) -> None:
		if not self.automation_enabled:
			return

		if not self.doctype_policies:
			frappe.msgprint(
				_("Document Automation is on but no document policies are set, so the assistant cannot touch anything."),
				title=_("No Document Policies"),
				indicator="orange",
			)

		if not self.require_user_mapping and not self.agent_user:
			frappe.throw(
				_(
					"Set a Default Acts As User, or turn on Only Act for Mapped Numbers. "
					"The assistant needs an identity to check permissions against."
				)
			)

	# ------------------------------------------------------------------ helpers

	def policy_for(self, doctype: str):
		"""The policy row for a doctype, or None when it is not automatable."""
		for row in self.doctype_policies:
			if row.document_type == doctype:
				return row
		return None

	def automatable_doctypes(self) -> list[str]:
		return [row.document_type for row in self.doctype_policies if row.can_read]

	def is_excluded(self, number: str) -> bool:
		return match_number(number, self.excluded_numbers)

	def is_allowed(self, number: str) -> bool:
		"""Whether this number may get an automated reply at all."""
		if self.reply_scope != "Only Allowed Numbers":
			return True
		return match_number(number, self.allowed_numbers)

	def user_for_number(self, number: str) -> str | None:
		"""The Frappe user a number acts as, from the allowed list."""
		from agent_x.core.phone import same_number

		for row in self.allowed_numbers:
			if row.user and same_number(number, row.phone_number):
				return row.user
		return None

	def is_within_business_hours(self, moment: datetime.datetime | None = None) -> bool:
		"""Whether `moment` falls in the working window.

		The window is read in the time zone from System Settings. `now_datetime`
		is already in that zone, so a bare call uses the site clock, not UTC.
		"""
		if not self.restrict_business_hours:
			return True

		moment = get_datetime(moment) if moment else now_datetime()

		allowed = {
			DAY_ALIASES[t.casefold()]
			for t in split_days(self.working_days)
			if t.casefold() in DAY_ALIASES
		}
		if allowed and moment.weekday() not in allowed:
			return False

		if not (self.business_hours_start and self.business_hours_end):
			return True

		start, end = to_time(self.business_hours_start), to_time(self.business_hours_end)
		now = moment.time()

		if start <= end:
			return start <= now <= end

		# The window wraps past midnight, e.g. 20:00 to 06:00.
		return now >= start or now <= end

	# ------------------------------------------------------------------ desk actions

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Check the provider is reachable and the credentials are accepted."""
		from agent_x.core import transport

		return transport.health(self)

	@frappe.whitelist()
	def register_webhook(self, session: str | None = None) -> dict:
		"""Tell the provider where to deliver inbound events.

		WaClient stores one URL per instance. The bridge stores one URL per
		session, so each tenant — including an ERPNext site on another server —
		receives only its own events.
		"""
		from agent_x.core import transport

		self.save()

		url = self.webhook_url
		provider = self.whatsapp_provider or "WaClient"

		if provider == "WaClient":
			self.warn_if_unreachable(url)
		else:
			self.warn_bridge_webhook(url)

		token = self.get_password("webhook_token", raise_exception=False)
		if token and provider == "WaClient":
			url = f"{url}?token={token}"

		if provider != "WaClient":
			# The bridge stores the webhook on the session, so one has to exist.
			session = session or self.session_for_setup().name

		return transport.get_transport(session=session, settings=self).register_webhook(url)

	def warn_if_unreachable(self, url: str) -> None:
		"""A provider on the internet cannot post to a private address."""
		if host_is_private(url):
			frappe.throw(
				_(
					"The webhook URL points at a private address ({0}), which a hosted provider "
					"cannot reach. Set Public Base URL to this site's public HTTPS address first."
				).format(url)
			)

		self.warn_if_http(url)

	def warn_bridge_webhook(self, url: str) -> None:
		"""A local bridge can call a local site. A public bridge cannot.

		ERPNext on another server reaches this bridge through the site's domain.
		Events go back to that server, so its webhook has to be public too.
		"""
		bridge_public = not host_is_private(self.bridge_url or "")
		if host_is_private(url) and bridge_public:
			frappe.throw(
				_(
					"The webhook URL points at a private address ({0}). The bridge is on a "
					"public host, so it cannot deliver events there. Set Public Base URL to "
					"this site's public HTTPS address."
				).format(url)
			)

		self.warn_if_http(url)

	def warn_if_http(self, url: str) -> None:
		from urllib.parse import urlparse

		if urlparse(url).scheme != "https":
			frappe.msgprint(
				_("{0} is plain HTTP. Most providers require HTTPS and will drop events.").format(url),
				title=_("Insecure Webhook URL"),
				indicator="orange",
			)

	@frappe.whitelist()
	def create_bridge_tenant(self, tenant_id: str) -> dict:
		"""Issue a token a cloud ERPNext site can use to log in to this bridge.

		The token is shown once. Paste it into that site's Bridge API Token,
		and set its Bridge URL to this site's public address plus /agentx-bridge.
		"""
		if (self.whatsapp_provider or "WaClient") != "Self-Hosted Bridge":
			frappe.throw(_("Switch the provider to Self-Hosted Bridge first."))

		from agent_x.core.transport.bridge import BridgeTransport

		tenant_id = (tenant_id or "").strip().lower()
		if not tenant_id:
			frappe.throw(_("Give the tenant a short id, such as the client's site name."))

		return BridgeTransport(None, self).create_tenant(tenant_id)

	@frappe.whitelist()
	def signup_on_bridge(self) -> dict:
		"""Create this site's client on the public bridge and store the token.

		No master token and no install command. The signed-in user's email
		receives the same details the bridge page would show.
		"""
		if (self.whatsapp_provider or "WaClient") != "Self-Hosted Bridge":
			frappe.throw(_("Switch the provider to Self-Hosted Bridge first."))

		base = (self.bridge_url or "").strip()
		if not base:
			frappe.throw(
				_("Set the Bridge URL to the public bridge, for example https://whatsapp.site.com.")
			)

		email = frappe.db.get_value("User", frappe.session.user, "email") or ""
		if "@" not in email:
			frappe.throw(_("Your user has no email address, so the bridge cannot send the client details."))

		from agent_x.core.transport.bridge import signup_client

		created = None
		client_id = _client_id()
		last_error = None
		for candidate in (client_id, f"{client_id}-{frappe.generate_hash(length=4)}"):
			try:
				created = signup_client(base, candidate, email, timeout=self.request_timeout or 30)
				break
			except Exception as exc:
				last_error = exc
				if "already exists" not in str(exc).lower():
					raise
		if not created:
			raise last_error

		if not self.get_password("webhook_secret", raise_exception=False):
			self.webhook_secret = frappe.generate_hash(length=32)
		self.bridge_api_token = created["token"]
		self.save()

		registered = self.register_webhook()
		return {
			"id": created.get("id"),
			"emailed": bool(created.get("emailed")),
			"webhook_verified": bool(registered.get("verified")),
		}

	# ------------------------------------------------------- the connection
	#
	# A WhatsApp Session is still the record that holds a linked number, but
	# nobody setting this up for the first time should have to know that. These
	# keep one session in step with the settings and drive pairing from here.

	DEFAULT_SESSION_NAME = "main"

	def session_for_setup(self, create: bool = True):
		"""The session these settings drive, made if it does not exist yet."""
		name = self.default_session

		if not name:
			name = frappe.db.get_value("WhatsApp Session", {"is_default": 1}, "name") or frappe.db.get_value(
				"WhatsApp Session", {}, "name"
			)

		if name and frappe.db.exists("WhatsApp Session", name):
			return frappe.get_doc("WhatsApp Session", name)

		if not create:
			return None

		doc = frappe.get_doc(
			{
				"doctype": "WhatsApp Session",
				"session_name": self.DEFAULT_SESSION_NAME,
				"enabled": 1,
				"is_default": 1,
				"instance_id": self.waclient_instance_id,
			}
		)
		doc.insert(ignore_permissions=True)

		self.db_set("default_session", doc.name, update_modified=False)
		frappe.db.commit()

		return doc

	def sync_session(self) -> None:
		"""Push the Instance ID onto the session these settings drive.

		Only fills a session that has none of its own, so a second number
		configured on its own session is never overwritten.
		"""
		if (self.whatsapp_provider or "WaClient") != "WaClient":
			return

		if not self.waclient_instance_id:
			return

		session = self.session_for_setup(create=False)
		if session and not session.instance_id:
			session.db_set("instance_id", self.waclient_instance_id, update_modified=False)

	@frappe.whitelist()
	def connect_whatsapp(self) -> dict:
		"""Start pairing, creating the session if this is the first time."""
		if (self.whatsapp_provider or "WaClient") == "WaClient" and not self.waclient_instance_id:
			frappe.throw(_("Enter the WaClient Instance ID first."))

		session = self.session_for_setup()
		result = session.connect()

		return {"session": session.name, **(result or {})}

	@frappe.whitelist()
	def connection_state(self) -> dict:
		"""What the connection panel shows."""
		session = self.session_for_setup(create=False)

		if not session:
			return {"configured": False, "state": "Not Set Up"}

		state = {
			"configured": True,
			"session": session.name,
			"state": session.state,
			"phone": session.phone_number,
			"qr": session.qr_data,
			"error": session.last_error,
			"instance_id": session.instance_id,
		}

		# Ask the provider rather than trusting what we last stored.
		try:
			live = session.refresh_status()
			state["state"] = session.state
			state["phone"] = live.get("phone") or state["phone"]
		except Exception as exc:
			state["error"] = str(exc)[:300]

		return state

	@frappe.whitelist()
	def disconnect_whatsapp(self) -> dict:
		session = self.session_for_setup(create=False)
		if not session:
			return {"state": "Not Set Up"}
		return session.logout()

	@frappe.whitelist()
	def test_ai(self, message: str = "Hello, are you there?") -> dict:
		"""Send one message to the model without touching WhatsApp."""
		from agent_x.agent import provider

		if not self.ai_enabled:
			frappe.throw(_("Enable the AI Assistant first."))

		return provider.ping(self, message)


def split_days(value: str | None) -> list[str]:
	return [t.strip() for t in (value or "").replace(";", ",").split(",") if t.strip()]


def to_time(value) -> datetime.time:
	value = get_time(value)
	return value if isinstance(value, datetime.time) else datetime.time(0, 0)


def match_number(number: str, rows) -> bool:
	from agent_x.core.phone import same_number

	return any(same_number(number, row.phone_number) for row in rows)


def _client_id() -> str:
	"""A bridge client id taken from this site's name."""
	import re

	raw = (getattr(frappe.local, "site", None) or "site").split(".", 1)[0].lower()
	cleaned = re.sub(r"[^a-z0-9]", "", raw)[:20]
	return cleaned or "site"


def host_is_private(url: str) -> bool:
	"""True when a machine off this network cannot open the address."""
	from urllib.parse import urlparse

	host = (urlparse(url or "").hostname or "").lower()
	if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "") or host.endswith(".localhost"):
		return True

	return host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19."))


def get_settings() -> "AgentXSettings":
	return frappe.get_cached_doc("AgentX Settings")
