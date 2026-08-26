"""The document tools the model may call.

Each function returns a plain dict the model can read back. Writes never happen
here directly: they go through an Agent Action, which is what actually applies
permissions and records the audit trail.
"""

import frappe
from frappe import _

from agent_x.agent import policy
from agent_x.agentx.doctype.agent_action.agent_action import create_action

# Never hand these back to a chat, whatever the doctype policy says.
SENSITIVE_FIELDTYPES = {"Password"}
HIDDEN_FIELDS = {"api_key", "api_secret", "password", "salt", "reset_password_key"}

MAX_ROWS = 20


class ToolContext:
	"""What the tools need to know about who is asking."""

	def __init__(self, settings, acting_user: str | None, contact=None, conversation=None, run=None):
		self.settings = settings
		self.acting_user = acting_user
		self.contact = contact
		self.conversation = conversation
		self.run = run

	@property
	def contact_name(self) -> str | None:
		return self.contact.name if self.contact else None

	@property
	def conversation_name(self) -> str | None:
		return self.conversation.name if self.conversation else None

	@property
	def run_name(self) -> str | None:
		return self.run.name if self.run else None


def readable_fields(doctype: str) -> set[str]:
	"""Every field we are willing to hand back, dropping anything secret."""
	meta = frappe.get_meta(doctype)

	usable = {"name", "owner", "creation", "modified", "docstatus"}
	for field in meta.fields:
		if field.fieldtype in SENSITIVE_FIELDTYPES:
			continue
		if field.fieldname in HIDDEN_FIELDS:
			continue
		# Layout elements carry no data.
		if field.fieldtype in ("Section Break", "Column Break", "Tab Break", "HTML", "Button"):
			continue
		usable.add(field.fieldname)

	return usable


def safe_fields(doctype: str, requested: list[str] | None) -> list[str]:
	"""Which fields to select for a list. Defaults to the list view columns."""
	usable = readable_fields(doctype)

	if not requested:
		meta = frappe.get_meta(doctype)
		defaults = ["name"]
		if meta.title_field:
			defaults.append(meta.title_field)
		defaults += [f.fieldname for f in meta.fields if f.in_list_view][:6]
		return list(dict.fromkeys(f for f in defaults if f in usable))

	unknown = [f for f in requested if f not in usable]
	if unknown:
		frappe.throw(
			_("These fields do not exist on {0}, or cannot be read: {1}").format(
				doctype, ", ".join(unknown)
			)
		)

	return requested


# ---------------------------------------------------------------------- reading


def list_documents(
	ctx: ToolContext,
	doctype: str,
	filters: dict | None = None,
	fields: list[str] | None = None,
	limit: int = 10,
	order_by: str | None = None,
) -> dict:
	"""Find documents matching some filters."""
	policy.check(ctx.settings, doctype, "read", ctx.acting_user).raise_if_denied()

	limit = max(1, min(int(limit or 10), MAX_ROWS))
	selected = safe_fields(doctype, fields)

	from agent_x.agentx.doctype.agent_action.agent_action import switch_user

	# get_all applies permissions and user permissions for the running user.
	with switch_user(ctx.acting_user):
		rows = frappe.get_all(
			doctype,
			filters=filters or None,
			fields=selected,
			limit=limit,
			order_by=order_by or "modified desc",
		)

	return {"doctype": doctype, "count": len(rows), "documents": rows}


def get_document(ctx: ToolContext, doctype: str, name: str, fields: list[str] | None = None) -> dict:
	"""Read one document."""
	policy.check(ctx.settings, doctype, "read", ctx.acting_user, name).raise_if_denied()

	from agent_x.agentx.doctype.agent_action.agent_action import switch_user

	with switch_user(ctx.acting_user):
		if not frappe.db.exists(doctype, name):
			return {"found": False, "doctype": doctype, "name": name}

		doc = frappe.get_doc(doctype, name)
		data = doc.as_dict(no_default_fields=False)

	# No field list means the whole document, which is what "get" should mean.
	selected = set(fields) if fields else readable_fields(doctype)
	if fields:
		safe_fields(doctype, fields)  # rejects unknown or secret field names

	# An unset field tells the model nothing and costs the same as a set one.
	# A whole Sales Order was 1,830 tokens, most of it nulls and zeroes.
	trimmed = {
		key: value
		for key, value in data.items()
		if (key in selected or key in ("name", "docstatus"))
		and value not in (None, "", [], {})
	}

	return {
		"found": True,
		"doctype": doctype,
		"name": name,
		"document": frappe.parse_json(frappe.as_json(trimmed)),
	}


def count_documents(ctx: ToolContext, doctype: str, filters: dict | None = None) -> dict:
	"""How many documents match."""
	policy.check(ctx.settings, doctype, "read", ctx.acting_user).raise_if_denied()

	from agent_x.agentx.doctype.agent_action.agent_action import switch_user

	with switch_user(ctx.acting_user):
		total = frappe.db.count(doctype, filters or None)

	return {"doctype": doctype, "count": total}


LAYOUT_FIELDTYPES = ("Section Break", "Column Break", "Tab Break", "HTML", "Button", "Fold")


def field_summary(field) -> dict:
	entry = {
		"fieldname": field.fieldname,
		"fieldtype": field.fieldtype,
	}

	# A label that is just the fieldname prettified tells the model nothing it
	# cannot already see, and every field pays for it.
	label = (field.label or "").strip()
	if label and label.lower().replace(" ", "_") != field.fieldname:
		entry["label"] = label

	if field.reqd:
		entry["required"] = True

	if field.fieldtype in ("Link", "Table", "Table MultiSelect"):
		entry["options"] = field.options
	elif field.fieldtype == "Select" and field.options:
		entry["choices"] = [o for o in field.options.split("\n") if o]

	if field.default and not str(field.default).startswith(":"):
		entry["default"] = field.default

	return entry


def usable_fields(meta) -> list:
	return [
		f
		for f in meta.fields
		if f.fieldtype not in LAYOUT_FIELDTYPES
		and f.fieldtype not in SENSITIVE_FIELDTYPES
		and f.fieldname not in HIDDEN_FIELDS
	]


# A doctype like Sales Order has 123 usable fields. Describing all of them cost
# 5,875 tokens, and the result was then truncated mid-list, which lost nine of
# the twelve required fields including the order lines themselves. The model was
# doing better guessing than reading. So: only what someone could actually fill
# in, required first so truncation can never reach them, and a hard cap.
MAX_FIELDS = 25
MAX_CHILD_FIELDS = 10

# Fields nobody sets by hand: computed, fetched from a link, or set on save.
def settable(field) -> bool:
	if field.read_only or field.hidden:
		return False
	if getattr(field, "is_virtual", 0):
		return False
	if field.fetch_from:
		# Filled in from a linked document.
		return False
	if field.fieldtype in LAYOUT_FIELDTYPES or field.fieldtype in SENSITIVE_FIELDTYPES:
		return False
	if field.fieldname in HIDDEN_FIELDS:
		return False
	return True


def by_importance(fields: list) -> list:
	"""Required first, then what the grid shows, then the rest."""
	def rank(field):
		if field.reqd:
			return 0
		if field.in_list_view:
			return 1
		if field.bold or field.fieldtype in ("Link", "Select", "Date", "Datetime"):
			return 2
		return 3

	return sorted(fields, key=lambda f: (rank(f), f.idx or 0))


def describe_child_table(child_doctype: str) -> dict:
	"""What one row of a child table needs.

	Without this the model can see that Sales Order has an `items` table but has
	no way to learn a row needs `item_code` and `qty`, and a child doctype is
	never in the policy list because nobody grants permissions on one directly.
	"""
	meta = frappe.get_meta(child_doctype)
	usable = [f for f in meta.fields if settable(f)]

	keep, seen = [], set()
	for field in by_importance(usable):
		if not (field.reqd or field.in_list_view):
			continue
		if field.fieldname in seen:
			continue
		seen.add(field.fieldname)
		keep.append(field_summary(field))
		if len(keep) >= MAX_CHILD_FIELDS:
			break

	return {"doctype": child_doctype, "fields": keep}


def describe_doctype(ctx: ToolContext, doctype: str) -> dict:
	"""What fields a doctype has, so the model can fill one in correctly.

	Deliberately partial. Everything the system fills in itself is left out, and
	what remains is ordered so the required fields are never the ones cut.
	"""
	policy.check(ctx.settings, doctype, "read", ctx.acting_user).raise_if_denied()

	meta = frappe.get_meta(doctype)
	usable = [f for f in meta.fields if settable(f)]
	ordered = by_importance(usable)

	fields, tables, omitted = [], {}, 0

	for field in ordered:
		# Line tables are the point of an order, so they are never dropped.
		is_table = field.fieldtype in ("Table", "Table MultiSelect")

		if len(fields) >= MAX_FIELDS and not (field.reqd or is_table):
			omitted += 1
			continue

		fields.append(field_summary(field))

		# Sales Order has seven child tables and only one of them is the order.
		# Expanding all of them was most of what pushed this past the size the
		# conversation can carry, so only the tables somebody actually fills in
		# are described.
		if is_table and field.options and field.options not in tables:
			if not (field.reqd or len(tables) < 1):
				continue
			try:
				tables[field.options] = describe_child_table(field.options)
			except Exception:
				frappe.log_error(
					frappe.get_traceback(), f"AgentX: could not describe {field.options}"
				)

	result = {
		"doctype": doctype,
		"is_submittable": bool(meta.is_submittable),
		"title_field": meta.title_field,
		"fields": fields,
		"child_tables": list(tables.values()),
		"note": _(
			"Required fields must be set unless the system fills them in. Send a child table "
			'as a list of objects, e.g. "items": [{"item_code": "ITEM-001", "qty": 2}]. '
			"Fields the system computes or fetches are not listed; do not invent them."
		),
	}

	if omitted:
		result["note"] += " " + _(
			"{0} less common fields were left out. Ask for them by name if you need one."
		).format(omitted)

	return result


def find_doctypes(ctx: ToolContext, query: str, limit: int = 10) -> dict:
	"""Look up document types by name.

	Only reachable in All Documents mode, where the model is not given a fixed
	list and would otherwise have to guess at names.
	"""
	from agent_x.agent import policy as policy_module

	if (ctx.settings.policy_mode or "Listed Documents Only") != "All Documents":
		return {"error": _("Ask about the document types you were told about.")}

	term = (query or "").strip()
	if not term:
		frappe.throw(_("Say what kind of document you are looking for."))

	rows = frappe.get_all(
		"DocType",
		filters={"name": ("like", f"%{term}%"), "issingle": 0, "istable": 0},
		fields=["name", "module", "is_submittable"],
		limit=max(1, min(int(limit or 10), 25)),
		order_by="name asc",
	)

	found = []
	for row in rows:
		if not policy_module.is_automatable(row.name):
			continue
		if not policy_module.has_permission_as(ctx.acting_user, row.name, "read", None):
			continue
		found.append(
			{"doctype": row.name, "module": row.module, "submittable": bool(row.is_submittable)}
		)

	return {
		"count": len(found),
		"doctypes": found,
		"note": _("Use describe_doctype on one of these to see its fields.")
		if found
		else _("Nothing matched that you are allowed to see."),
	}


# ---------------------------------------------------------------------- writing


def propose(ctx: ToolContext, action: str, doctype: str, name: str | None, payload: dict | None, summary: str) -> dict:
	"""Shared path for every write: check policy, then record an Agent Action."""
	decision = policy.check(ctx.settings, doctype, action, ctx.acting_user, name)
	decision.raise_if_denied()

	if payload and action in ("create", "update"):
		payload = policy.allowed_fields(decision.policy, doctype, payload)

	if over_conversation_limit(ctx):
		frappe.throw(
			_("This conversation has already made the maximum number of changes. Start a new one.")
		)

	doc = create_action(
		action=action,
		document_type=doctype,
		document_name=name,
		payload=payload,
		summary=summary,
		acting_user=ctx.acting_user,
		contact=ctx.contact_name,
		conversation=ctx.conversation_name,
		agent_run=ctx.run_name,
		status="Pending",
	)

	if decision.needs_approval:
		# Hand it back for a human yes. The runtime turns this into a question.
		return {
			"status": "awaiting_confirmation",
			"action_id": doc.name,
			"summary": summary,
			"message": _("Ask the customer to confirm before this happens."),
		}

	result = doc.execute()
	bump_conversation(ctx)

	# Same courtesy when no confirmation was required: a long document goes back
	# as a PDF so the customer can still check it.
	from agent_x.agent import drafts

	session = ctx.conversation.session if ctx.conversation else None
	if drafts.maybe_send(doc, ctx.settings, ctx.contact, session, ctx.run_name):
		result["draft_sent"] = True

	return {"status": "done", "action_id": doc.name, **result}


def create_document(ctx: ToolContext, doctype: str, values: dict, summary: str | None = None) -> dict:
	"""Create a document."""
	if not isinstance(values, dict) or not values:
		frappe.throw(_("Creating a {0} needs at least one field value.").format(doctype))

	return propose(
		ctx,
		"create",
		doctype,
		None,
		values,
		summary or _("Create a new {0}").format(doctype),
	)


def update_document(ctx: ToolContext, doctype: str, name: str, values: dict, summary: str | None = None) -> dict:
	"""Change fields on an existing document."""
	if not isinstance(values, dict) or not values:
		frappe.throw(_("Updating {0} needs at least one field to change.").format(name))

	return propose(
		ctx,
		"update",
		doctype,
		name,
		values,
		summary or _("Update {0} {1}").format(doctype, name),
	)


def submit_document(ctx: ToolContext, doctype: str, name: str, summary: str | None = None) -> dict:
	"""Submit a draft."""
	return propose(ctx, "submit", doctype, name, None, summary or _("Submit {0} {1}").format(doctype, name))


def cancel_document(ctx: ToolContext, doctype: str, name: str, summary: str | None = None) -> dict:
	"""Cancel a submitted document."""
	return propose(ctx, "cancel", doctype, name, None, summary or _("Cancel {0} {1}").format(doctype, name))


def delete_document(ctx: ToolContext, doctype: str, name: str, summary: str | None = None) -> dict:
	"""Delete a document."""
	return propose(ctx, "delete", doctype, name, None, summary or _("Delete {0} {1}").format(doctype, name))


# ------------------------------------------------------------------- sending


def send_document(
	ctx: ToolContext,
	doctype: str,
	name: str,
	message: str | None = None,
	print_format: str | None = None,
) -> dict:
	"""Send the customer a PDF of a document, using the site's print format.

	Reading is the right gate here: the PDF shows what the document says, so
	anyone allowed to read it is allowed to be sent it. Nothing is written, so
	this never needs approval.
	"""
	decision = policy.check(ctx.settings, doctype, "read", ctx.acting_user, name)
	decision.raise_if_denied()

	if not ctx.settings.allow_document_pdfs:
		frappe.throw(_("Sending document PDFs is switched off in AgentX Settings."))

	if not ctx.contact:
		frappe.throw(_("There is nobody to send this to."))

	if not frappe.db.exists(doctype, name):
		return {"sent": False, "error": _("There is no {0} called {1}.").format(doctype, name)}

	from agent_x.agentx.doctype.agent_action.agent_action import switch_user
	from agent_x.core import printing
	from agent_x.core.messaging import send_message
	from agent_x.core.transport import get_transport

	chosen_format = print_format or (decision.policy.print_format if decision.policy else None)

	session = ctx.conversation.session if ctx.conversation else None
	transport = get_transport(session=session, settings=ctx.settings)

	# Render as the acting user so the print format sees only what they may see.
	with switch_user(ctx.acting_user):
		prepared = printing.prepare(
			doctype,
			name,
			needs_public_url=transport.needs_public_media,
			print_format=chosen_format,
		)

	result = send_message(
		ctx.contact.wa_id,
		message or _("Here is {0} {1}.").format(doctype, name),
		session=session,
		media_url=prepared.get("url"),
		media_base64=prepared.get("base64"),
		media_kind="document",
		media_filename=prepared["filename"],
		media_mimetype="application/pdf",
		reference_doctype=doctype,
		reference_name=name,
		agent_run=ctx.run_name,
		settings=ctx.settings,
	)

	return {
		"sent": True,
		"doctype": doctype,
		"name": name,
		"print_format": prepared["print_format"],
		"filename": prepared["filename"],
		"message_id": result.get("message_id"),
	}


# ---------------------------------------------------------------------- limits


def over_conversation_limit(ctx: ToolContext) -> bool:
	cap = ctx.settings.max_actions_per_conversation or 0
	if cap <= 0 or not ctx.conversation:
		return False

	return (ctx.conversation.action_count or 0) >= cap


def bump_conversation(ctx: ToolContext) -> None:
	if not ctx.conversation:
		return

	ctx.conversation.db_set(
		"action_count", (ctx.conversation.action_count or 0) + 1, update_modified=False
	)
