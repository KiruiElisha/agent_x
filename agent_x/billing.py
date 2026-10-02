"""IntaSend subscriptions, configured on AgentX Settings.

Collects one M-Pesa prompt per period, or a card subscription through
https://developers.intasend.com/guides/subscriptions
Webhooks are accepted only when the challenge matches.
"""

from datetime import timedelta
import hmac

import requests

import frappe
from frappe.utils import get_url, now_datetime

COLLECTION = "https://api.intasend.com/api/v1/"
SANDBOX = "https://sandbox.intasend.com/api/v1/"


def base_url(settings) -> str:
	return SANDBOX if settings.intasend_sandbox else COLLECTION


def secret(settings) -> str:
	return settings.get_password("intasend_secret_key", raise_exception=False) or ""


def challenge(settings) -> str:
	return settings.get_password("intasend_webhook_challenge", raise_exception=False) or ""


def period(settings) -> timedelta:
	count = int(settings.subscription_frequency or 1)
	unit = settings.subscription_frequency_unit or "M"
	days = {"D": count, "W": count * 7, "M": count * 30, "Y": count * 365}.get(unit, 30)
	return timedelta(days=max(days, 1))


def call(settings, path: str, payload: dict | None = None, method: str = "POST") -> dict:
	key = secret(settings)
	if not key:
		frappe.throw("Set the IntaSend secret key in AgentX Settings.")
	response = requests.request(
		method,
		base_url(settings) + path.lstrip("/"),
		headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
		json=payload,
		timeout=settings.request_timeout or 30,
	)
	try:
		body = response.json()
	except ValueError:
		body = {"detail": (response.text or "")[:300]}
	if response.status_code >= 400:
		frappe.throw(f"IntaSend refused the request: {body}")
	return body if isinstance(body, dict) else {}


def upsert(email: str, **values) -> "frappe.model.document.Document":
	email = (email or "").strip().lower()
	if frappe.db.exists("Bridge Subscription", email):
		doc = frappe.get_doc("Bridge Subscription", email)
		doc.update(values)
		doc.save(ignore_permissions=True)
		return doc
	doc = frappe.get_doc({"doctype": "Bridge Subscription", "email": email, **values})
	doc.insert(ignore_permissions=True)
	return doc


def push_to_bridge(settings) -> None:
	"""Copy the plan onto the bridge so the public page can start a payment."""
	url = (settings.bridge_url or "").strip().rstrip("/")
	token = settings.get_password("bridge_api_token", raise_exception=False) if url else ""
	if not (url and token):
		return
	payload = {
		"enabled": bool(settings.subscriptions_enabled),
		"required": bool(settings.subscriptions_required),
		"sandbox": bool(settings.intasend_sandbox),
		"secret_key": secret(settings),
		"challenge": challenge(settings),
		"amount": settings.subscription_amount or 0,
		"currency": settings.intasend_currency or "KES",
		"method": settings.subscription_method or "M-Pesa prompt",
		"plan_name": settings.subscription_plan_name or "WhatsApp API",
		"frequency": int(settings.subscription_frequency or 1),
		"frequency_unit": settings.subscription_frequency_unit or "M",
		"billing_cycles": int(settings.subscription_billing_cycles or 0),
		"period_days": period(settings).days,
		"notify_url": settings.intasend_webhook_url or get_url("/api/method/agent_x.billing.intasend"),
		"site_url": site_sync_url(),
	}
	try:
		response = requests.post(
			f"{url}/api/billing/config",
			headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
			json=payload,
			timeout=settings.request_timeout or 30,
		)
	except requests.RequestException as exc:
		frappe.msgprint(
			f"Subscriptions were saved here, but the bridge did not accept them: {exc}",
			indicator="orange",
			title="Bridge",
		)
		return
	if response.status_code >= 400:
		frappe.msgprint(
			f"Subscriptions were saved here, but the bridge returned {response.status_code}.",
			indicator="orange",
			title="Bridge",
		)


def site_sync_url() -> str:
	return get_url("/api/method/agent_x.billing.sync_client")


def remember_site(settings) -> None:
	"""Tell the bridge where new signups should be copied."""
	url = (settings.bridge_url or "").strip().rstrip("/")
	token = settings.get_password("bridge_api_token", raise_exception=False) if url else ""
	if not (url and token):
		return
	try:
		requests.post(
			f"{url}/api/billing/config",
			headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
			json={"site_url": site_sync_url()},
			timeout=settings.request_timeout or 30,
		)
	except requests.RequestException:
		frappe.log_error(frappe.get_traceback(), "AgentX: could not register this site with the bridge")


def ensure_customer(email: str, client_id: str = "") -> str | None:
	"""A bridge client is also a Customer, when ERPNext is installed."""
	if not frappe.db.exists("DocType", "Customer"):
		return None
	label = email.split("@", 1)[0] if email and "@" in email and not email.endswith("@clients") else (client_id or email)
	name = None
	if email:
		name = frappe.db.get_value("Customer", {"customer_name": email}) or frappe.db.get_value("Customer", {"name": email})
	if not name and label:
		name = frappe.db.get_value("Customer", {"customer_name": label})
	if name:
		return name
	group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups"
	territory = frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories"
	doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": (label or email)[:140],
			"customer_type": "Individual",
			"customer_group": group,
			"territory": territory,
		}
	)
	try:
		doc.insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "AgentX: could not create a customer for a bridge client")
		return None
	if frappe.db.exists("DocType", "Contact") and "@" in (email or "") and not str(email).endswith("@clients"):
		try:
			frappe.get_doc(
				{
					"doctype": "Contact",
					"first_name": (label or "Client")[:140],
					"email_ids": [{"email_id": email, "is_primary": 1}],
					"links": [{"link_doctype": "Customer", "link_name": doc.name}],
				}
			).insert(ignore_permissions=True)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "AgentX: could not attach an email to a bridge customer")
	return doc.name


def record_client(email: str, client_id: str = "", payment: str = "", paid_until=None, trial_until=None) -> None:
	"""Create or refresh the subscription row for one bridge client.

	A missing payment does not change an existing row. Completed payments stay
	Active until the bridge explicitly reports Trial or Active.
	"""
	email = (email or "").strip().lower()
	if not email and client_id:
		email = f"{client_id.strip().lower()}@clients"
	if not email:
		return
	customer = ensure_customer(email, client_id)
	until = paid_until if payment == "Active" else trial_until if payment == "Trial" else None
	known = payment in ("Active", "Trial")
	if frappe.db.exists("Bridge Subscription", email):
		doc = frappe.get_doc("Bridge Subscription", email)
		changed = False
		if client_id and doc.client_id != client_id:
			doc.client_id = client_id
			changed = True
		if customer and doc.customer != customer:
			doc.customer = customer
			changed = True
		if known and doc.status != payment:
			doc.status = payment
			changed = True
		if known and until and str(doc.paid_until or "") != str(until):
			doc.paid_until = until
			changed = True
		if changed:
			doc.save(ignore_permissions=True)
		return
	values = {"client_id": client_id or None, "status": payment if known else "Signed up", "customer": customer}
	if known and until:
		values["paid_until"] = until
	upsert(email, **values)


@frappe.whitelist()
def pull_clients(settings=None) -> dict:
	"""Copy bridge clients into Bridge Subscription. Existing payments are left as they are."""
	explicit = settings is not None
	settings = settings or frappe.get_single("AgentX Settings")
	url = (settings.bridge_url or "").strip().rstrip("/")
	token = settings.get_password("bridge_api_token", raise_exception=False) if url else ""
	if not (url and token):
		return {"synced": 0}
	quiet = explicit
	try:
		response = requests.get(
			f"{url}/api/tenants",
			headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
			timeout=settings.request_timeout or 30,
		)
	except requests.RequestException as exc:
		if quiet:
			frappe.log_error(frappe.get_traceback(), "AgentX: could not read clients from the bridge")
		else:
			frappe.msgprint(f"Could not read clients from the bridge: {exc}", indicator="orange", title="Bridge")
		return {"synced": 0}
	if response.status_code >= 400:
		if quiet:
			frappe.log_error(f"bridge clients returned {response.status_code}", "AgentX: client sync")
		else:
			frappe.msgprint(
				f"The bridge refused the client list ({response.status_code}).",
				indicator="orange",
				title="Bridge",
			)
		return {"synced": 0}
	rows = (response.json() or {}).get("tenants") or []
	if not rows:
		try:
			richer = requests.get(
				f"{url}/api/billing/clients",
				headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
				timeout=settings.request_timeout or 30,
			)
			if richer.status_code < 400:
				rows = (richer.json() or {}).get("clients") or rows
		except requests.RequestException:
			pass
	count = 0
	for row in rows:
		if not isinstance(row, dict):
			continue
		if not (row.get("email") or row.get("id")):
			continue
		record_client(
			row.get("email"),
			row.get("id") or "",
			payment=row.get("payment") or "",
			paid_until=row.get("paid_until"),
			trial_until=row.get("trial_until"),
		)
		count += 1
	frappe.db.commit()
	return {"synced": count}


@frappe.whitelist()
def bridge_control(command: str) -> dict:
	"""Run one command on the bridge server: status, logs, start, stop, restart, backup, or update."""
	if "System Manager" not in frappe.get_roles():
		frappe.throw("Only a System Manager can control the bridge.", frappe.PermissionError)
	allowed = {"status", "logs", "start", "stop", "restart", "backup", "update"}
	if command not in allowed:
		frappe.throw(f"That bridge command is not available: {command}")
	settings = frappe.get_single("AgentX Settings")
	url = (settings.bridge_url or "").strip().rstrip("/")
	token = settings.get_password("bridge_api_token", raise_exception=False) if url else ""
	if not (url and token):
		frappe.throw("Set the bridge URL and API token first.")
	try:
		response = requests.post(
			f"{url}/api/control",
			headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
			json={"command": command},
			timeout=40,
		)
	except requests.RequestException as exc:
		frappe.throw(f"The bridge did not answer: {exc}")
	try:
		body = response.json()
	except ValueError:
		body = {}
	if response.status_code >= 400 or body.get("ok") is False:
		frappe.throw(body.get("error") or body.get("message") or f"The bridge refused that ({response.status_code}).")
	return body


@frappe.whitelist(allow_guest=True, methods=["POST"])
def sync_client() -> dict:
	"""Bridge signup notification. The bearer token must be the bridge API token."""
	settings = frappe.get_single("AgentX Settings")
	expected = settings.get_password("bridge_api_token", raise_exception=False) or ""
	header = frappe.get_request_header("Authorization") or ""
	if not expected or not hmac.compare_digest(header, f"Bearer {expected}"):
		frappe.local.response["http_status_code"] = 401
		return {"status": "unauthorised"}
	raw = frappe.request.get_json(silent=True) if frappe.request else None
	body = raw if isinstance(raw, dict) else {}
	record_client(
		body.get("email"),
		body.get("client_id") or "",
		payment=body.get("payment") or "",
		trial_until=body.get("trial_until"),
		paid_until=body.get("paid_until"),
	)
	frappe.db.commit()
	return {"status": "ok"}


def grant(email: str, *, phone: str = "", invoice_id: str = "", subscription_id: str = "", state: str = "", setup_url: str = "") -> None:
	settings = frappe.get_cached_doc("AgentX Settings")
	until = now_datetime() + period(settings)
	upsert(
		email,
		phone=phone or None,
		status="Active",
		amount=settings.subscription_amount,
		currency=settings.intasend_currency or "KES",
		paid_until=until,
		invoice_id=invoice_id or None,
		subscription_id=subscription_id or None,
		last_state=state or "COMPLETE",
		setup_url=setup_url or None,
	)
	token = settings.get_password("bridge_api_token", raise_exception=False)
	url = (settings.bridge_url or "").strip().rstrip("/")
	if url and token:
		try:
			requests.post(
				f"{url}/api/billing/grant",
				headers={"Authorization": f"Bearer {token}"},
				json={
					"email": email,
					"paid_until": until.isoformat(),
					"amount": settings.subscription_amount,
					"currency": settings.intasend_currency or "KES",
					"invoice_id": invoice_id,
					"plan_name": settings.subscription_plan_name or "WhatsApp API",
				},
				timeout=15,
			)
		except requests.RequestException:
			frappe.log_error(frappe.get_traceback(), "AgentX: could not tell the bridge about a subscription")
	frappe.db.commit()


@frappe.whitelist()
def collect(email: str, phone: str | None = None) -> dict:
	"""Start a payment for one client. Called from the subscription form."""
	settings = frappe.get_cached_doc("AgentX Settings")
	if not settings.subscriptions_enabled:
		frappe.throw("Turn on subscriptions in AgentX Settings first.")
	email = (email or "").strip().lower()
	phone = "".join(c for c in str(phone or "") if c.isdigit())
	upsert(email, phone=phone or None, status="Pending", amount=settings.subscription_amount, currency=settings.intasend_currency or "KES")
	result = _charge(settings, email, phone)
	frappe.db.commit()
	return result


def _charge(settings, email: str, phone: str) -> dict:
	ref = f"sub:{email}"
	if (settings.subscription_method or "M-Pesa prompt") == "Card subscription":
		customer = call(
			settings,
			"subscriptions-customers/",
			{
				"email": email,
				"first_name": (email.split("@", 1)[0] or "Client")[:40],
				"last_name": "Client",
				"country": "KE",
				"reference": ref,
			},
		)
		plan = call(
			settings,
			"subscriptions-plans/",
			{
				"name": settings.subscription_plan_name or "WhatsApp API",
				"amount": settings.subscription_amount,
				"currency": settings.intasend_currency or "KES",
				"frequency": int(settings.subscription_frequency or 1),
				"frequency_unit": settings.subscription_frequency_unit or "M",
				"billing_cycles": int(settings.subscription_billing_cycles or 0),
			},
		)
		created = call(
			settings,
			"subscriptions/",
			{
				"customer_id": customer.get("customer_id"),
				"plan_id": plan.get("plan_id"),
				"reference": ref,
			},
		)
		upsert(email, subscription_id=created.get("id") or created.get("subscription_id"), setup_url=created.get("setup_url"), last_state="PENDING")
		return {"setup_url": created.get("setup_url"), "subscription_id": created.get("id") or created.get("subscription_id")}

	if not phone:
		frappe.throw("An M-Pesa phone number is required.")
	body = call(settings, "payment/mpesa-stk-push/", {"amount": settings.subscription_amount, "phone_number": phone, "api_ref": ref})
	invoice = (body.get("invoice") or {}).get("invoice_id")
	upsert(email, phone=phone, invoice_id=invoice, last_state=(body.get("invoice") or {}).get("state") or "PENDING")
	return {"invoice_id": invoice, "state": (body.get("invoice") or {}).get("state")}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def intasend() -> dict:
	"""IntaSend webhook. The challenge must match AgentX Settings."""
	settings = frappe.get_cached_doc("AgentX Settings")
	expected = challenge(settings)
	raw = frappe.request.get_json(silent=True) if frappe.request else None
	body = raw if isinstance(raw, dict) else {}
	provided = str(body.get("challenge") or "")
	if not expected or not hmac.compare_digest(provided, expected):
		frappe.local.response["http_status_code"] = 401
		return {"status": "unauthorised"}

	reference = str(body.get("reference") or body.get("api_ref") or "")
	customer = body.get("customer") if isinstance(body.get("customer"), dict) else {}
	email = str(body.get("email") or customer.get("email") or "").strip().lower()
	if "@" not in email and reference.startswith("sub:"):
		email = reference.split("sub:", 1)[1].strip().lower()
	if not email:
		return {"status": "ignored"}

	payments = body.get("payments") if isinstance(body.get("payments"), list) else []
	invoice = body.get("invoice") if isinstance(body.get("invoice"), dict) else {}
	if payments and isinstance(payments[-1], dict) and isinstance(payments[-1].get("invoice"), dict):
		invoice = payments[-1]["invoice"]
	state = str(body.get("status") or invoice.get("state") or body.get("state") or "")
	if state == "COMPLETE" or state == "ACTIVE":
		grant(
			email,
			invoice_id=str(invoice.get("invoice_id") or body.get("invoice_id") or ""),
			subscription_id=str(body.get("subscription_id") or body.get("id") or ""),
			state=state,
		)
		return {"status": "active"}
	if state in ("FAILED", "CANCELED", "CANCELLED"):
		upsert(email, status="Past Due" if state == "FAILED" else "Cancelled", last_state=state)
		frappe.db.commit()
	return {"status": "ok", "state": state}
