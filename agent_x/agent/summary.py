"""Renders what is about to happen, in words a customer can check.

The model writes the conversation, but it must never be the source of the
numbers someone is agreeing to. Everything here is read back out of the payload
or the saved document, so a confirmation shows what will actually be written,
not what the model remembers writing.
"""

import frappe
from frappe import _
from frappe.utils import flt, fmt_money

# WhatsApp accepts about 4096 characters. Leave room for the closing question.
CONFIRMATION_LIMIT = 3500

# A long order still has to be readable, so past this many lines it is summarised.
MAX_LINES_SHOWN = 25

# Fields worth naming when there is no line table to show.
PARTY_FIELDS = ("customer", "customer_name", "supplier", "supplier_name", "party_name", "lead_name")
TOTAL_FIELDS = ("grand_total", "rounded_total", "total", "amount")


def line_table(doctype: str, payload: dict) -> tuple[str, list] | tuple[None, None]:
	"""The child table in this payload that holds the lines, if there is one.

	Recognised by shape rather than by looking the doctype up: a list of objects
	is a line table whatever it is called, and a confirmation must still render
	when the meta cannot be loaded.
	"""
	for fieldname, value in (payload or {}).items():
		if isinstance(value, list) and value and isinstance(value[0], dict):
			return fieldname, value

	return None, None


def describe_line(index: int, row: dict, currency: str | None) -> tuple[str, float]:
	"""One order line, and what it costs."""
	code = row.get("item_code") or row.get("item") or row.get("description") or _("item")
	name = row.get("item_name")
	qty = flt(row.get("qty") or row.get("quantity") or 1)
	rate = row.get("rate")

	label = f"{code}"
	if name and name != code:
		label = f"{code} ({name})"

	if rate is None:
		return f"{index}. {label} x{qty:g}", 0.0

	amount = flt(rate) * qty
	return (
		f"{index}. {label} x{qty:g} @ {money(rate, currency)} = {money(amount, currency)}",
		amount,
	)


def money(value, currency: str | None) -> str:
	try:
		return fmt_money(flt(value), currency=currency)
	except Exception:
		return f"{flt(value):,.2f}"


def describe_payload(doctype: str, payload: dict) -> str:
	"""What a create or update is going to write."""
	currency = payload.get("currency")
	parts = []

	party = next((payload[f] for f in PARTY_FIELDS if payload.get(f)), None)
	heading = f"{doctype}"
	if party:
		heading += f" {_('for')} {party}"
	parts.append(heading)

	fieldname, rows = line_table(doctype, payload)

	if rows:
		total = 0.0
		shown = rows[:MAX_LINES_SHOWN]

		for index, row in enumerate(shown, 1):
			if not isinstance(row, dict):
				continue
			text, amount = describe_line(index, row, currency)
			parts.append(text)
			total += amount

		# A long order is still checkable if the tail is counted rather than listed.
		hidden = len(rows) - len(shown)
		if hidden > 0:
			remainder = sum(
				flt(r.get("rate") or 0) * flt(r.get("qty") or 1)
				for r in rows[MAX_LINES_SHOWN:]
				if isinstance(r, dict)
			)
			total += remainder
			parts.append(_("...and {0} more lines.").format(hidden))

		parts.append(_("{0} lines").format(len(rows)))
		if total:
			parts.append(_("Total: {0}").format(money(total, currency)))

		return "\n".join(parts)

	# No lines, so name the fields being set instead.
	skip = {"doctype", "naming_series"}
	details = [
		f"{key}: {value}"
		for key, value in payload.items()
		if key not in skip and not isinstance(value, (list, dict)) and value not in (None, "")
	][:12]

	parts.extend(details)
	return "\n".join(parts)


def describe_document(doctype: str, name: str) -> str:
	"""What an existing document says, for submit, cancel, and delete."""
	try:
		doc = frappe.get_doc(doctype, name)
	except Exception:
		doc = None

	if doc is None:
		# Better to name the document plainly than to fail the whole confirmation.
		return f"{doctype} {name}"

	parts = [f"{doctype} {name}"]

	party = next((doc.get(f) for f in PARTY_FIELDS if doc.get(f)), None)
	if party:
		parts.append(_("For: {0}").format(party))

	currency = doc.get("currency")

	for field in doc.meta.fields:
		if field.fieldtype != "Table":
			continue
		rows = doc.get(field.fieldname) or []
		if not rows:
			continue

		shown = rows[:MAX_LINES_SHOWN]
		for index, row in enumerate(shown, 1):
			text, _amount = describe_line(index, row.as_dict(), currency)
			parts.append(text)

		if len(rows) > len(shown):
			parts.append(_("...and {0} more lines.").format(len(rows) - len(shown)))

		parts.append(_("{0} lines").format(len(rows)))
		break

	total = next((doc.get(f) for f in TOTAL_FIELDS if doc.get(f)), None)
	if total:
		parts.append(_("Total: {0}").format(money(total, currency)))

	return "\n".join(parts)


ASK = {
	"create": "Reply YES to create it, or NO to drop it.",
	"update": "Reply YES to make this change, or NO to leave it alone.",
	"submit": "Reply YES to submit it, or NO to leave it as a draft.",
	"cancel": "Reply YES to cancel it, or NO to leave it as it is.",
	"delete": "Reply YES to delete it permanently, or NO to keep it.",
}

LEAD = {
	"create": "I am about to create this:",
	"update": "I am about to change this:",
	"submit": "I am about to submit this:",
	"cancel": "I am about to cancel this:",
	"delete": "I am about to permanently delete this:",
}


def for_action(action) -> str:
	"""The message a customer sees before anything is written."""
	payload = action.get_payload()

	if action.action in ("submit", "cancel", "delete") and action.document_name:
		body = describe_document(action.document_type, action.document_name)
	elif payload:
		body = describe_payload(action.document_type, payload)
	else:
		body = f"{action.document_type} {action.document_name or ''}".strip()

	text = "\n".join([_(LEAD.get(action.action, "Please confirm:")), "", body, "", _(ASK.get(action.action, "Reply YES to go ahead, or NO to stop."))])

	if len(text) > CONFIRMATION_LIMIT:
		text = text[:CONFIRMATION_LIMIT].rsplit("\n", 1)[0] + "\n\n" + _(ASK.get(action.action, "Reply YES to go ahead."))

	return text
