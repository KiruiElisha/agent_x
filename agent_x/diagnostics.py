"""Checks every link in the chain between WhatsApp and a reply.

When nothing arrives there are a dozen possible causes and no way to tell them
apart from the desk. This walks the chain in order and reports the first thing
that is actually wrong, rather than leaving it to guesswork.
"""

import frappe
from frappe import _
from frappe.utils import get_url, now_datetime, add_to_date


def check(label: str, ok: bool | None, detail: str = "", fix: str = "") -> dict:
	return {"check": label, "ok": ok, "detail": detail, "fix": fix}


@frappe.whitelist()
def run() -> dict:
	"""Walk the whole path and report what is broken."""
	frappe.only_for("System Manager")

	settings = frappe.get_cached_doc("AgentX Settings")
	results = []

	results += basics(settings)
	results += credentials(settings)
	results += connection(settings)
	results += webhook(settings)
	results += traffic(settings)
	results += recent_errors()

	failed = [r for r in results if r["ok"] is False]

	return {
		"ok": not failed,
		"summary": _("Everything checks out.")
		if not failed
		else _("{0} problem(s) found. The first one is usually the cause.").format(len(failed)),
		"results": results,
	}


def basics(settings) -> list:
	out = [
		check("AgentX enabled", bool(settings.enabled), fix=_("Tick Enable AgentX on the Connection tab.")),
		check("AI assistant enabled", bool(settings.ai_enabled), fix=_("Tick Enable AI Assistant on the AI tab.")),
	]

	if settings.ai_enabled:
		has_key = bool(settings.get_password("ai_api_key", raise_exception=False))
		out.append(check("AI API key set", has_key, fix=_("Paste your Gemini API key on the AI tab.")))

	if settings.restrict_business_hours:
		inside = settings.is_within_business_hours()
		out.append(
			check(
				"Within business hours",
				inside,
				detail="" if inside else _("Outside hours, so only the outside-hours reply is sent."),
				fix=_("Turn off Restrict to Business Hours while testing."),
			)
		)

	return out


def credentials(settings) -> list:
	provider = settings.whatsapp_provider or "WaClient"
	out = [check("Provider", True, provider)]

	if provider == "WaClient":
		out.append(
			check(
				"Instance ID set",
				bool((settings.waclient_instance_id or "").strip()),
				settings.waclient_instance_id or "",
				_("Copy it from the WaClient dashboard into AgentX Settings."),
			)
		)
		out.append(
			check(
				"Access token set",
				bool(settings.get_password("waclient_access_token", raise_exception=False)),
				fix=_("Copy your WaClient access token into AgentX Settings."),
			)
		)
	else:
		out.append(check("Bridge URL set", bool(settings.bridge_url), settings.bridge_url or ""))
		out.append(
			check(
				"Bridge token set",
				bool(settings.get_password("bridge_api_token", raise_exception=False)),
			)
		)

	return out


def connection(settings) -> list:
	"""Is a phone actually linked, according to the provider."""
	from agent_x.core.transport import get_transport

	session = settings.default_session or frappe.db.get_value(
		"WhatsApp Session", {"is_default": 1}, "name"
	)

	if not session:
		return [
			check(
				"WhatsApp session exists",
				False,
				fix=_("Press Connect WhatsApp on the Connection tab."),
			)
		]

	try:
		status = get_transport(session=session, settings=settings).status()
	except Exception as exc:
		return [
			check(
				"Provider reachable",
				False,
				str(exc)[:200],
				_("Check the instance ID and access token."),
			)
		]

    # A session that is not connected cannot receive or send anything.
	state = status.get("state")
	return [
		check("Provider reachable", True, session),
		check(
			"Phone linked",
			state == "connected",
			_("State is {0}{1}").format(state, f", number {status.get('phone')}" if status.get("phone") else ""),
			_("Press Connect WhatsApp and scan the QR code."),
		),
	]


def webhook(settings) -> list:
	"""The most common cause of total silence: the provider is not posting to us."""
	from agent_x.core.transport import get_transport

	ours = settings.webhook_url or get_url("/api/method/agent_x.core.webhook.receive")
	out = [check("Our webhook URL", True, ours)]

	local = any(
		host in ours for host in ("localhost", "127.0.0.1", ".local", "0.0.0.0")
	)
	if local:
		out.append(
			check(
				"Webhook URL is public",
				False,
				ours,
				_("Set Public Base URL to this site's public address, then register again."),
			)
		)
		return out

	session = settings.default_session
	if not session:
		return out

	try:
		transport = get_transport(session=session, settings=settings)
		registered = transport.get_webhook() if hasattr(transport, "get_webhook") else {}
	except Exception as exc:
		out.append(
			check(
				"Webhook registered with provider",
				None,
				_("Could not read it back: {0}").format(str(exc)[:150]),
				_("Press Register Webhook on the Connection tab."),
			)
		)
		return out

	their_url = (registered or {}).get("webhook_url") or ""
	enabled = bool((registered or {}).get("enabled"))

	out.append(
		check(
			"Webhook registered with provider",
			bool(their_url),
			their_url or _("nothing registered"),
			_("Press Register Webhook on the Connection tab."),
		)
	)

	if their_url:
		out.append(
			check(
				"Registered URL matches ours",
				their_url.split("?")[0] == ours.split("?")[0],
				_("Provider has: {0}").format(their_url),
				_("Press Register Webhook to point it here."),
			)
		)
		out.append(
			check(
				"Webhook enabled at provider",
				enabled,
				fix=_("Press Register Webhook again; it must be enabled, not just saved."),
			)
		)

	return out


def traffic(settings) -> list:
	"""Has anything ever arrived, and was it answered."""
	since = add_to_date(now_datetime(), days=-7)

	incoming = frappe.db.count("WhatsApp Message", {"direction": "Incoming", "creation": (">", since)})
	outgoing = frappe.db.count("WhatsApp Message", {"direction": "Outgoing", "creation": (">", since)})
	runs = frappe.db.count("Agent Run", {"creation": (">", since)})

	out = [
		check(
			"Inbound messages received (7 days)",
			incoming > 0,
			str(incoming),
			_("Nothing has arrived. That points at the webhook, not the assistant."),
		),
		check("Replies sent (7 days)", None, str(outgoing)),
		check("Agent runs (7 days)", None, str(runs)),
	]

	if settings.reply_scope == "Only Allowed Numbers":
		numbers = [r.phone_number for r in settings.allowed_numbers]
		out.append(
			check(
				"Allowed numbers configured",
				bool(numbers),
				", ".join(numbers) or _("none"),
				_("Add the number you are messaging from, with its country code."),
			)
		)

		if incoming:
			# A number that wrote in but is not on the list is a silent refusal.
			senders = frappe.get_all(
				"WhatsApp Message",
				filters={"direction": "Incoming", "creation": (">", since)},
				pluck="wa_id",
				limit=20,
			)
			blocked = sorted({s for s in senders if s and not settings.is_allowed(s)})
			if blocked:
				out.append(
					check(
						"All senders are allowed",
						False,
						_("These wrote in but are not on the list: {0}").format(", ".join(blocked)),
						_("Add them to Allowed Numbers, or set Reply To to Everyone."),
					)
				)

	if settings.only_verified_customers:
		out.append(
			check(
				"Only Serve Verified Customers",
				None,
				_("On. Numbers that do not belong to a Customer get {0}.").format(
					_("the set reply") if settings.unverified_reply else _("no reply at all")
				),
				_("Turn it off, or link the number to a Customer."),
			)
		)

		if incoming:
			senders = frappe.get_all(
				"WhatsApp Message",
				filters={"direction": "Incoming", "creation": (">", since)},
				pluck="contact",
				limit=20,
			)
			from agent_x.agent.tools import customers

			unknown = []
			for name in {s for s in senders if s}:
				try:
					contact = frappe.get_doc("WhatsApp Contact", name)
					if not customers.resolve_for_contact(contact, auto_link=False):
						unknown.append(name)
				except Exception:
					continue

			if unknown:
				out.append(
					check(
						"Recent senders are known customers",
						False,
						_("Not linked to a Customer: {0}").format(", ".join(sorted(unknown)[:5])),
						_("Link them to a Customer, or turn off Only Serve Verified Customers."),
					)
				)

	if settings.require_user_mapping:
		unmapped = [r.phone_number for r in settings.allowed_numbers if not r.user]
		if unmapped:
			out.append(
				check(
					"Allowed numbers mapped to a user",
					False,
					", ".join(unmapped),
					_("Set Acts As User on each allowed number, or turn off Only Act for Mapped Numbers."),
				)
			)

	return out


def recent_errors() -> list:
	since = add_to_date(now_datetime(), days=-2)
	rows = frappe.get_all(
		"Error Log",
		filters={"creation": (">", since), "error": ("like", "%agent_x%")},
		fields=["name", "method", "creation"],
		order_by="creation desc",
		limit=5,
	)

	if not rows:
		return [check("Recent errors", True, _("none in the last 2 days"))]

	return [
		check(
			"Recent errors",
			False,
			"; ".join(f"{r.method or r.name}" for r in rows)[:300],
			_("Open the Error Log for the full traceback."),
		)
	]
