"""Outbound alerts: WhatsApp messages the system starts, rather than replies to.

An order confirmation, a delivery notice, a payment reminder. These hang off
document events, so the dispatcher runs on every save on the site and has to be
close to free for the overwhelming majority of documents that have no alert.
That is what the cached doctype set below is for: one Redis read, then return.

Sending happens in a background job. A WhatsApp API call has no business
sitting inside someone's save.
"""

import frappe
from frappe import _
from frappe.utils import add_days, getdate, now_datetime

CACHE_KEY = "agentx:alerts:doctypes"

DOC_EVENTS = {
	"after_insert": "After Insert",
	"on_submit": "On Submit",
	"on_cancel": "On Cancel",
	"on_update": "On Update",
}

SCHEDULED_EVENTS = ("Days Before", "Days After")


# ------------------------------------------------------------------ dispatch


def watched_doctypes() -> set:
	"""Which doctypes have any enabled alert.

	This runs on every document save on the site, so it is cached twice: in
	request local memory, and in Redis behind that. Only the first save in a
	request touches Redis, and only a cold cache touches the database.

	A failed lookup is cached too. Without that, a site with the doctype not yet
	migrated pays a failed query on every save, which measured at 6ms each.
	"""
	local = getattr(frappe.local, "agentx_alert_doctypes", None)
	if local is not None:
		return local

	found = None
	try:
		cached = frappe.cache.get_value(CACHE_KEY)
		if cached is not None:
			found = set(cached)
	except Exception:
		pass

	if found is None:
		try:
			rows = frappe.get_all(
				"WhatsApp Alert", filters={"enabled": 1}, pluck="document_type", distinct=True
			)
			found = {r for r in rows if r}
		except Exception:
			# The doctype may not exist yet, during install or before migrate.
			found = set()

		try:
			frappe.cache.set_value(CACHE_KEY, list(found), expires_in_sec=3600)
		except Exception:
			pass

	frappe.local.agentx_alert_doctypes = found
	return found


def clear_cache() -> None:
	try:
		frappe.cache.delete_value(CACHE_KEY)
	except Exception:
		pass

	# The request that changed an alert must not keep serving the old set.
	if hasattr(frappe.local, "agentx_alert_doctypes"):
		del frappe.local.agentx_alert_doctypes


def handle(doc, method: str) -> None:
	"""Entry point from doc_events. Must stay cheap and must never raise."""
	event = DOC_EVENTS.get(method)
	if not event:
		return

	# Our own records must not trigger alerts about themselves.
	if doc.doctype.startswith(("WhatsApp ", "Agent ", "AgentX ")):
		return

	if doc.doctype not in watched_doctypes():
		return

	try:
		run_for_document(doc, event)
	except Exception:
		# A failed alert must never roll back the document that triggered it.
		frappe.log_error(frappe.get_traceback(), f"AgentX: alert dispatch failed for {doc.doctype}")


def run_for_document(doc, event: str) -> None:
	settings = frappe.get_cached_doc("AgentX Settings")
	if not (settings.enabled and settings.alerts_enabled):
		return

	events = [event]
	# A field change is also an update, so both are considered together.
	if event == "On Update":
		events.append("On Value Change")

	alerts = frappe.get_all(
		"WhatsApp Alert",
		filters={"enabled": 1, "document_type": doc.doctype, "event": ("in", events)},
		pluck="name",
	)

	for name in alerts:
		alert = frappe.get_cached_doc("WhatsApp Alert", name)

		if alert.event == "On Value Change" and not value_changed(doc, alert.value_change_field):
			continue

		if not passes_condition(alert, doc):
			continue

		enqueue_send(alert.name, doc.doctype, doc.name)


def value_changed(doc, fieldname: str | None) -> bool:
	"""Whether the watched field differs from what is in the database."""
	if not fieldname:
		return False

	before = doc.get_doc_before_save()
	if not before:
		return False

	return before.get(fieldname) != doc.get(fieldname)


def passes_condition(alert, doc) -> bool:
	condition = (alert.condition or "").strip()
	if not condition:
		return True

	try:
		# safe_eval blocks imports, attribute traversal into builtins, and the
		# usual escapes, so an operator cannot turn a condition into a shell.
		return bool(frappe.safe_eval(condition, None, {"doc": doc, "frappe": frappe._dict()}))
	except Exception:
		frappe.log_error(
			f"{frappe.get_traceback()}\n\nCondition: {condition}",
			f"AgentX: alert condition failed ({alert.name})",
		)
		return False


def enqueue_send(alert: str, doctype: str, docname: str) -> None:
	frappe.enqueue(
		"agent_x.core.alerts.send",
		queue="short",
		timeout=300,
		enqueue_after_commit=True,
		alert=alert,
		doctype=doctype,
		docname=docname,
	)


# ---------------------------------------------------------------- recipients


def resolve_number(alert, doc) -> str | None:
	"""Where this alert goes."""
	if alert.recipient_type == "Fixed Number":
		return alert.fixed_number

	if alert.recipient_type == "Linked Contact":
		return linked_contact_number(doc)

	path = (alert.recipient_field or "").strip()
	if not path:
		return None

	# A dotted path reads through a link field, e.g. customer.mobile_no.
	value = doc
	for part in path.split("."):
		if value is None:
			return None
		if isinstance(value, str):
			# The previous hop was a link, so load it before going further.
			return None
		value = value.get(part) if hasattr(value, "get") else getattr(value, part, None)

		if isinstance(value, str) and "." in path and part != path.split(".")[-1]:
			link_doctype = link_target(doc, part)
			if not link_doctype:
				return None
			value = frappe.get_cached_doc(link_doctype, value)

	return value if isinstance(value, str) else None


def link_target(doc, fieldname: str) -> str | None:
	field = doc.meta.get_field(fieldname)
	return field.options if field and field.fieldtype == "Link" else None


def linked_contact_number(doc) -> str | None:
	"""The WhatsApp Contact tied to the party on this document."""
	for field in ("customer", "supplier", "lead", "party"):
		value = doc.get(field)
		if not value:
			continue

		number = frappe.db.get_value("WhatsApp Contact", {field: value}, "wa_id")
		if number:
			return number

	return None


# -------------------------------------------------------------------- sending


def already_sent(alert: str, doctype: str, docname: str) -> bool:
	"""Providers retry and schedulers repeat, so never send the same alert twice."""
	return bool(
		frappe.db.exists(
			"WhatsApp Message",
			{
				"alert": alert,
				"reference_doctype": doctype,
				"reference_name": docname,
				"status": ("!=", "Failed"),
			},
		)
	)


def send(alert: str, doctype: str, docname: str) -> dict | None:
	"""Render and deliver one alert. Runs in the background."""
	from agent_x.core.messaging import send_message

	settings = frappe.get_cached_doc("AgentX Settings")
	if not (settings.enabled and settings.alerts_enabled):
		return None

	if not frappe.db.exists("WhatsApp Alert", alert):
		return None

	alert_doc = frappe.get_cached_doc("WhatsApp Alert", alert)
	if not alert_doc.enabled:
		return None

	if already_sent(alert, doctype, docname):
		return None

	try:
		doc = frappe.get_doc(doctype, docname)
	except frappe.DoesNotExistError:
		return None

	if alert_doc.respect_business_hours and not settings.is_within_business_hours():
		# Retried by the scheduler rather than sent at 3am.
		return {"deferred": True}

	number = resolve_number(alert_doc, doc)
	if not number:
		record_failure(alert_doc, _("No phone number found on {0} {1}.").format(doctype, docname))
		return None

	if settings.is_excluded(number):
		return None

	try:
		message = frappe.render_template(alert_doc.message, {"doc": doc, "alert": alert_doc})
	except Exception as exc:
		record_failure(alert_doc, _("Message template failed: {0}").format(str(exc)[:200]))
		return None

	media = {}
	if alert_doc.attach_document and settings.allow_document_pdfs:
		media = attachment_for(alert_doc, doc, settings)

	try:
		result = send_message(
			number,
			message,
			session=alert_doc.session,
			reference_doctype=doctype,
			reference_name=docname,
			settings=settings,
			**media,
		)
	except Exception as exc:
		record_failure(alert_doc, str(exc)[:300])
		return None

	if result.get("log"):
		frappe.db.set_value("WhatsApp Message", result["log"], "alert", alert, update_modified=False)

	alert_doc.db_set(
		{"sent_count": (alert_doc.sent_count or 0) + 1, "last_sent_on": now_datetime(), "last_error": None},
		update_modified=False,
	)
	frappe.db.commit()

	return result


def attachment_for(alert, doc, settings) -> dict:
	"""Render the document as a PDF to go with the message."""
	from agent_x.core import printing
	from agent_x.core.transport import get_transport

	try:
		transport = get_transport(session=alert.session, settings=settings)
		prepared = printing.prepare(
			doc.doctype,
			doc.name,
			needs_public_url=transport.needs_public_media,
			print_format=alert.print_format,
		)
		return {
			"media_url": prepared.get("url"),
			"media_base64": prepared.get("base64"),
			"media_kind": "document",
			"media_filename": prepared["filename"],
			"media_mimetype": "application/pdf",
		}
	except Exception:
		# Better to send the message without the PDF than not at all.
		frappe.log_error(frappe.get_traceback(), f"AgentX: could not attach a PDF to {alert.name}")
		return {}


def record_failure(alert, message: str) -> None:
	alert.db_set(
		{"error_count": (alert.error_count or 0) + 1, "last_error": message},
		update_modified=False,
	)
	frappe.db.commit()


# ------------------------------------------------------------------ scheduled


def run_scheduled() -> None:
	"""Date based alerts: reminders before or after a field on the document.

	Scheduled hourly so a business-hours hold is picked up the same day.
	"""
	settings = frappe.get_cached_doc("AgentX Settings")
	if not (settings.enabled and settings.alerts_enabled):
		return

	if not settings.is_within_business_hours():
		# Everything here is a courtesy message; none of it is urgent enough
		# to wake somebody up.
		return

	alerts = frappe.get_all(
		"WhatsApp Alert",
		filters={"enabled": 1, "event": ("in", SCHEDULED_EVENTS)},
		pluck="name",
	)

	for name in alerts:
		try:
			run_one_scheduled(frappe.get_cached_doc("WhatsApp Alert", name))
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"AgentX: scheduled alert failed ({name})")


def target_date(event: str, days: int | None, today):
	"""Which date a document must carry for its reminder to be due today.

	Easy to get backwards. "3 days before the due date" fires when the due date
	is three days in the future, so we look for due_date == today + 3. "2 days
	after delivery" fires when delivery was two days ago, so today - 2.
	"""
	count = abs(days or 0)
	offset = count if event == "Days Before" else -count
	return add_days(today, offset)


def run_one_scheduled(alert) -> None:
	if not alert.date_field:
		return

	target = target_date(alert.event, alert.days, getdate())

	try:
		meta = frappe.get_meta(alert.document_type)
		field = meta.get_field(alert.date_field)
	except Exception:
		return

	if not field or field.fieldtype not in ("Date", "Datetime"):
		record_failure(alert, _("{0} is not a Date field on {1}.").format(alert.date_field, alert.document_type))
		return

	filters = {alert.date_field: ("between", [f"{target} 00:00:00", f"{target} 23:59:59"])} \
		if field.fieldtype == "Datetime" else {alert.date_field: target}

	# Cancelled documents should not chase anyone.
	if meta.is_submittable:
		filters["docstatus"] = 1

	for row in frappe.get_all(alert.document_type, filters=filters, pluck="name", limit=500):
		if already_sent(alert.name, alert.document_type, row):
			continue

		doc = frappe.get_doc(alert.document_type, row)
		if not passes_condition(alert, doc):
			continue

		enqueue_send(alert.name, alert.document_type, row)
