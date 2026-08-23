"""Picks the WhatsApp provider named in AgentX Settings.

Everything above this line — the agent, the policy gate, the audit trail — is
written against `Transport` and never learns which provider is in use.
"""

import requests

import frappe
from frappe import _

from agent_x.core.transport.base import (  # noqa: F401  (re-exported)
	NotConnected,
	Transport,
	TransportError,
	normalise_state,
	qr_to_data_url,
	render_qr,
)

WACLIENT = "WaClient"
BRIDGE = "Self-Hosted Bridge"


def get_settings():
	return frappe.get_cached_doc("AgentX Settings")


def get_transport(session: str | None = None, settings=None) -> Transport:
	"""Build the transport for a session."""
	settings = settings or get_settings()

	if not settings.enabled:
		frappe.throw(_("AgentX is disabled in AgentX Settings."))

	session = session or settings.default_session or default_session()
	provider = settings.whatsapp_provider or WACLIENT

	if provider == WACLIENT:
		from agent_x.core.transport.waclient import WaClientTransport

		return WaClientTransport(session, settings)

	if provider == BRIDGE:
		from agent_x.core.transport.bridge import BridgeTransport

		return BridgeTransport(session, settings)

	frappe.throw(_("Unknown WhatsApp provider: {0}").format(provider))


def default_session() -> str | None:
	from agent_x.agentx.doctype.whatsapp_session.whatsapp_session import get_default_session

	return get_default_session()


def health(settings=None) -> dict:
	"""Is the provider reachable at all? Used by the Test Connection button."""
	settings = settings or get_settings()
	provider = settings.whatsapp_provider or WACLIENT

	if provider == BRIDGE:
		base = (settings.bridge_url or "").strip().rstrip("/")
		if not base:
			frappe.throw(_("Set the Bridge URL in AgentX Settings."))

		try:
			response = requests.get(f"{base}/health", timeout=settings.request_timeout or 30)
			response.raise_for_status()
			return {"provider": provider, "reachable": True, **response.json()}
		except requests.RequestException as exc:
			return {"provider": provider, "reachable": False, "error": str(exc)}

	# WaClient has no unauthenticated health endpoint, so a status call is the check.
	try:
		status = get_transport(settings=settings).status()
		return {"provider": provider, "reachable": True, **status}
	except Exception as exc:
		return {"provider": provider, "reachable": False, "error": str(exc)}
