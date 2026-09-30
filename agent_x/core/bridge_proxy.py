"""Publish the local bridge on a site that already has a domain.

ERPNext on another server, including Frappe Cloud, cannot reach 127.0.0.1.
A request to ``https://<this-site>/agentx-bridge/...`` is forwarded to the
bridge on loopback. The bridge still checks the bearer token, so this path
adds reachability and not a second login.
"""

import requests
from werkzeug.exceptions import HTTPException
from werkzeug.wrappers import Response

import frappe

PREFIX = "/agentx-bridge"
UPSTREAM = "http://127.0.0.1:8787"

# Headers the bridge actually reads. Everything else stays on this site.
FORWARDED = ("Authorization", "Content-Type", "Accept", "X-Api-Token")


class Proxied(HTTPException):
	"""Stops Frappe routing and returns the bridge response as-is."""

	def __init__(self, response: Response):
		super().__init__()
		self.response = response

	def get_response(self, environ=None, scope=None):
		return self.response


def maybe_proxy() -> None:
	"""Hook: before_request. Ignore every path that is not the bridge prefix."""
	request = getattr(frappe, "request", None) or getattr(frappe.local, "request", None)
	if request is None:
		return

	path = request.path or ""
	if path != PREFIX and not path.startswith(PREFIX + "/"):
		return

	upstream = _upstream()
	target = upstream + path[len(PREFIX) :]
	if request.query_string:
		target = f"{target}?{request.query_string.decode()}"

	headers = {}
	for name in FORWARDED:
		value = request.headers.get(name)
		if value:
			headers[name] = value

	try:
		upstream_response = requests.request(
			request.method,
			target,
			data=request.get_data(),
			headers=headers,
			timeout=60,
			allow_redirects=False,
		)
	except requests.RequestException as exc:
		body = f'{{"ok": false, "error": "WhatsApp bridge is not reachable: {exc}"}}'
		raise Proxied(Response(body, status=502, content_type="application/json")) from exc

	raise Proxied(
		Response(
			upstream_response.content,
			status=upstream_response.status_code,
			content_type=upstream_response.headers.get("Content-Type", "application/json"),
		)
	)


def _upstream() -> str:
	configured = ""
	try:
		configured = (frappe.conf.get("agentx_bridge_upstream") or "").strip().rstrip("/")
	except Exception:
		configured = ""
	return configured or UPSTREAM
