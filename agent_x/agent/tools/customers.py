"""Working out which Customer a WhatsApp contact is.

The number is the only thing known for certain at the start of a conversation,
so the search goes outward from it: the link already stored on the contact, then
anywhere in the system that records a phone number, then the name they use on
WhatsApp. Only when all of that comes up empty is the customer asked.

Guessing wrong here is expensive. An order raised against the wrong customer
bills the wrong company, so a single strong match is proposed rather than
assumed, and several matches are always put back to the customer.
"""

import frappe
from frappe import _

from agent_x.core.phone import digits_only, same_number

# Doctypes that commonly hold a phone number against a party.
PHONE_FIELDS = ("mobile_no", "phone", "phone_no", "contact_mobile", "contact_phone")


def guard(ctx) -> None:
	from agent_x.agent import policy

	if not frappe.db.exists("DocType", "Customer"):
		frappe.throw(_("This site has no Customer records."))

	policy.check(ctx.settings, "Customer", "read", ctx.acting_user).raise_if_denied()


def existing_link(contact) -> str | None:
	"""What the contact record already says, which beats any search."""
	if contact and contact.customer and frappe.db.exists("Customer", contact.customer):
		return contact.customer
	return None


def by_phone(number: str) -> list[str]:
	"""Every customer reachable from this number, by any route the site uses."""
	digits = digits_only(number)
	if not digits:
		return []

	# The last nine digits identify a subscriber regardless of how the country
	# code was stored, which is the usual reason a lookup misses.
	tail = digits[-9:] if len(digits) >= 9 else digits
	like = f"%{tail}"

	found = set()

	# 1. A Contact linked to a Customer, which is where ERPNext normally puts it.
	rows = frappe.db.sql(
		"""
		SELECT dl.link_name
		FROM `tabDynamic Link` dl
		INNER JOIN `tabContact` c ON c.name = dl.parent
		LEFT JOIN `tabContact Phone` p ON p.parent = c.name
		WHERE dl.link_doctype = 'Customer'
		  AND dl.parenttype = 'Contact'
		  AND (c.mobile_no LIKE %(like)s OR c.phone LIKE %(like)s OR p.phone LIKE %(like)s)
		""",
		{"like": like},
		as_dict=True,
	)
	found.update(r.link_name for r in rows if r.link_name)

	# 2. A phone number stored straight on the Customer.
	meta = frappe.get_meta("Customer")
	for field in PHONE_FIELDS:
		if meta.get_field(field):
			found.update(
				frappe.get_all(
					"Customer", filters={field: ("like", like)}, pluck="name", limit=10
				)
			)

	# 3. An Address carrying the number.
	if frappe.db.exists("DocType", "Address"):
		rows = frappe.db.sql(
			"""
			SELECT dl.link_name
			FROM `tabDynamic Link` dl
			INNER JOIN `tabAddress` a ON a.name = dl.parent
			WHERE dl.link_doctype = 'Customer'
			  AND dl.parenttype = 'Address'
			  AND (a.phone LIKE %(like)s)
			""",
			{"like": like},
			as_dict=True,
		)
		found.update(r.link_name for r in rows if r.link_name)

	# A disabled customer should not be proposed as if it were usable.
	return [c for c in found if not frappe.db.get_value("Customer", c, "disabled")]


def by_name(name: str, limit: int = 5) -> list[str]:
	cleaned = (name or "").strip()
	if len(cleaned) < 3:
		return []

	return frappe.get_all(
		"Customer",
		filters={"disabled": 0},
		or_filters={
			"customer_name": ("like", f"%{cleaned}%"),
			"name": ("like", f"%{cleaned}%"),
		},
		pluck="name",
		limit=limit,
	)


def describe(names: list[str]) -> list[dict]:
	if not names:
		return []

	rows = frappe.get_all(
		"Customer",
		filters={"name": ("in", names)},
		fields=["name", "customer_name", "customer_group", "territory", "mobile_no"]
		if frappe.get_meta("Customer").get_field("mobile_no")
		else ["name", "customer_name", "customer_group", "territory"],
	)
	return [{k: v for k, v in r.items() if v} for r in rows]


# ------------------------------------------------------------------ tools


def find_customer(ctx, query: str | None = None) -> dict:
	"""Work out who this conversation is for.

	Called with no query it resolves from the contact and the phone number.
	Called with a query it searches by name as well, for when the customer says
	who they are.
	"""
	guard(ctx)

	contact = ctx.contact
	number = contact.wa_id if contact else None

	linked = existing_link(contact)
	if linked and not query:
		return {
			"status": "linked",
			"customer": linked,
			"details": describe([linked]),
			"note": _("This contact is already linked to that customer. Use it."),
		}

	from_phone = by_phone(number) if number else []
	from_name = by_name(query) if query else []

	# A name search only narrows what the phone already suggested; it never
	# introduces a customer the number does not point at, unless the phone
	# search found nothing at all.
	if from_phone and from_name:
		overlap = [c for c in from_phone if c in from_name]
		candidates = overlap or from_phone
	else:
		candidates = from_phone or from_name

	if not candidates:
		return {
			"status": "none",
			"searched_number": number,
			"note": _(
				"No customer matches this number. Ask whether they already have an account "
				"under a different number, or whether to create a new one. Do not create it "
				"without asking."
			),
		}

	if len(candidates) == 1 and (from_phone or query):
		return {
			"status": "one_match",
			"customer": candidates[0],
			"details": describe(candidates),
			"matched_on": "phone" if from_phone else "name",
			"note": _(
				"Confirm this is them before ordering, then call link_customer to remember it."
			),
		}

	return {
		"status": "several",
		"customers": describe(candidates[:5]),
		"note": _("Ask which of these they are. Do not choose for them."),
	}


def link_customer(ctx, customer: str) -> dict:
	"""Remember which customer this number belongs to."""
	guard(ctx)

	if not frappe.db.exists("Customer", customer):
		return {"ok": False, "error": _("There is no customer called {0}.").format(customer)}

	if frappe.db.get_value("Customer", customer, "disabled"):
		return {"ok": False, "error": _("{0} is disabled and cannot be used.").format(customer)}

	if not ctx.contact:
		return {"ok": False, "error": _("There is no contact to link.")}

	ctx.contact.db_set("customer", customer, update_modified=False)
	frappe.db.commit()

	return {
		"ok": True,
		"customer": customer,
		"note": _("Remembered. Future orders from this number will use it without asking."),
	}


def create_customer(ctx, customer_name: str, customer_group: str | None = None,
                    territory: str | None = None) -> dict:
	"""Create a customer, once they have said that is what they want.

	Goes through the normal write path, so the policy table and the acting
	user's permissions both apply, and it is confirmed like any other change.
	"""
	name = (customer_name or "").strip()
	if len(name) < 2:
		frappe.throw(_("A customer needs a name."))

	# Creating a second account for someone who already has one causes real
	# damage, so refuse rather than let the model retry blindly.
	clash = by_name(name)
	if clash:
		return {
			"ok": False,
			"status": "already_exists",
			"customers": describe(clash[:5]),
			"note": _("Someone with that name already exists. Ask if it is them before creating another."),
		}

	from agent_x.agent.tools.documents import create_document

	values = {"customer_name": name}
	if customer_group:
		values["customer_group"] = customer_group
	if territory:
		values["territory"] = territory

	return create_document(
		ctx, "Customer", values, summary=_("Create a customer called {0}").format(name)
	)


# --------------------------------------------------------------- verification


def resolve_for_contact(contact, auto_link: bool = True) -> str | None:
	"""The customer this contact belongs to, if one can be established.

	Used by the Only Serve Verified Customers gate. A real customer whose number
	is already on file should not be turned away merely because nobody has
	linked their WhatsApp contact yet, so a single unambiguous phone match is
	linked automatically. Several matches are left alone: picking one here would
	be guessing, and the assistant can ask instead.
	"""
	if not contact:
		return None

	linked = existing_link(contact)
	if linked:
		return linked

	if contact.is_group:
		# A group is not a person, so there is nobody to verify.
		return None

	matches = by_phone(contact.wa_id)
	if len(matches) != 1:
		return None

	customer = matches[0]

	if auto_link:
		try:
			contact.db_set("customer", customer, update_modified=False)
			frappe.db.commit()
		except Exception:
			# Recognising them matters more than remembering them.
			frappe.log_error(frappe.get_traceback(), "AgentX: could not link a contact")

	return customer


def is_verified(contact) -> bool:
	return bool(resolve_for_contact(contact))

