"""Decides whether the assistant may do something, and whether a human must confirm.

Two independent gates, and both must pass:

  1. The AgentX policy: an administrator listed this doctype and ticked this
     operation. Nothing outside the policy table is reachable at all.
  2. Frappe's own permissions, checked as the contact's mapped user.

The first is a deliberately small allowlist; the second means a mapped user can
never do more through WhatsApp than they could do in the desk.
"""

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

from agent_x.agentx.doctype.agentx_settings.agentx_settings import FORBIDDEN_DOCTYPES

# Operation to the policy field and the Frappe permission it needs.
OPERATIONS = {
	"read": ("can_read", "read"),
	"create": ("can_create", "create"),
	"update": ("can_write", "write"),
	"submit": ("can_submit", "submit"),
	"cancel": ("can_cancel", "cancel"),
	"delete": ("can_delete", "delete"),
}

WRITE_OPERATIONS = ("create", "update", "submit", "cancel", "delete")


class PolicyError(frappe.ValidationError):
	"""The assistant is not allowed to do this."""


class Decision:
	"""The outcome of a policy check."""

	def __init__(self, allowed: bool, reason: str = "", needs_approval: bool = False, policy=None):
		self.allowed = allowed
		self.reason = reason
		self.needs_approval = needs_approval
		self.policy = policy

	def __bool__(self) -> bool:
		return self.allowed

	def raise_if_denied(self) -> None:
		if not self.allowed:
			raise PolicyError(self.reason)


def check(
	settings,
	doctype: str,
	operation: str,
	acting_user: str | None,
	docname: str | None = None,
) -> Decision:
	"""Can the assistant perform `operation` on `doctype` as `acting_user`?"""
	if operation not in OPERATIONS:
		return Decision(False, _("Unknown operation: {0}").format(operation))

	if not settings.automation_enabled:
		return Decision(False, _("Document automation is switched off."))

	if doctype in FORBIDDEN_DOCTYPES:
		return Decision(False, _("{0} can never be automated.").format(doctype))

	if not acting_user:
		return Decision(
			False,
			_("This number is not linked to a user, so it cannot read or change documents."),
		)

	policy = settings.policy_for(doctype)

	if not policy:
		# In All Documents mode anything not listed falls back to the defaults,
		# still bounded by the forbidden list and by Frappe's own permissions.
		policy = default_policy(settings, doctype)

	if not policy:
		return Decision(
			False,
			_("{0} is not in the list of documents the assistant may use.").format(doctype),
		)

	policy_field, permission = OPERATIONS[operation]
	if not policy.get(policy_field):
		return Decision(False, _("The assistant may not {0} {1}.").format(operation, doctype))

	# Now the real permission check, as the user this conversation acts for.
	if not has_permission_as(acting_user, doctype, permission, docname):
		return Decision(
			False,
			_("{0} does not have permission to {1} {2}.").format(acting_user, permission, doctype),
		)

	if operation in WRITE_OPERATIONS:
		over_limit = daily_limit_reached(policy, doctype)
		if over_limit:
			return Decision(False, over_limit)

	needs_approval = bool(
		operation in WRITE_OPERATIONS
		and (policy.requires_approval or settings.confirm_before_write)
	)

	return Decision(True, needs_approval=needs_approval, policy=policy)


class DefaultPolicy:
	"""Stands in for a policy row when All Documents mode is on.

	Reads the same field names a real row has, so nothing downstream has to
	know which kind it received.
	"""

	def __init__(self, settings, doctype: str):
		self.document_type = doctype
		self.can_read = settings.all_can_read
		self.can_create = settings.all_can_create
		self.can_write = settings.all_can_write
		self.can_submit = settings.all_can_submit
		self.can_cancel = settings.all_can_cancel
		self.can_delete = settings.all_can_delete
		self.requires_approval = settings.all_requires_approval
		self.max_per_day = settings.all_max_per_day
		# No field narrowing and no print format override for unlisted doctypes;
		# to restrict either, add an explicit row.
		self.allowed_fields = ""
		self.print_format = None
		self.notes = ""

	def get(self, key, default=None):
		return getattr(self, key, default)


def default_policy(settings, doctype: str):
	"""The fallback permissions for a doctype nobody listed."""
	if (settings.policy_mode or "Listed Documents Only") != "All Documents":
		return None

	if not is_automatable(doctype):
		return None

	return DefaultPolicy(settings, doctype)


# Child tables are edited through their parent, and a Single has no list to
# work from, so neither is a sensible thing to hand a chat assistant.
def is_automatable(doctype: str) -> bool:
	if doctype in FORBIDDEN_DOCTYPES:
		return False

	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return False

	if meta.istable:
		return False

	# Anything holding a password field is excluded whatever the mode, because
	# a reader could ask for it by name.
	if any(f.fieldtype == "Password" for f in meta.fields):
		return False

	return True


def has_permission_as(user: str, doctype: str, permission: str, docname: str | None) -> bool:
	"""Frappe's permission check, evaluated as another user."""
	try:
		doc = frappe.get_doc(doctype, docname) if docname else None
	except frappe.DoesNotExistError:
		return False

	return bool(frappe.has_permission(doctype, permission, doc=doc, user=user))


def daily_limit_reached(policy, doctype: str) -> str:
	"""Empty string when under the cap, otherwise the reason to refuse."""
	cap = policy.max_per_day or 0
	if cap <= 0:
		return ""

	since = add_to_date(now_datetime(), days=-1)
	filters = {"status": "Executed", "creation": (">", since)}

	# A listed doctype is capped on its own. The All Documents default is a
	# single budget shared by everything unlisted, or it would be no cap at all.
	if not isinstance(policy, DefaultPolicy):
		filters["document_type"] = doctype

	used = frappe.db.count("Agent Action", filters)

	if used >= cap:
		if isinstance(policy, DefaultPolicy):
			return _("The daily limit of {0} automated actions has been reached.").format(cap)
		return _("The daily limit of {0} automated actions on {1} has been reached.").format(
			cap, doctype
		)
	return ""


def allowed_fields(policy, doctype: str, payload: dict) -> dict:
	"""Drop fields the policy does not permit.

	An empty Allowed Fields means "whatever the user could already write", which
	Frappe enforces on save, so only a non-empty list narrows anything here.
	"""
	raw = (policy.allowed_fields or "").strip() if policy else ""
	if not raw:
		return payload

	allowed = {f.strip() for f in raw.split(",") if f.strip()}
	blocked = set(payload) - allowed

	if blocked:
		raise PolicyError(
			_("The assistant may only set these fields on {0}: {1}. It tried to set {2}.").format(
				doctype, ", ".join(sorted(allowed)), ", ".join(sorted(blocked))
			)
		)

	return payload


def describe_for_prompt(settings) -> str:
	"""Tell the model what it may touch, so it does not propose the impossible."""
	if not settings.automation_enabled:
		return "You cannot read or change any documents. Answer from the business context only."

	if (settings.policy_mode or "Listed Documents Only") == "All Documents":
		return describe_all_mode(settings)

	if not settings.doctype_policies:
		return "You cannot read or change any documents. Answer from the business context only."

	lines = ["You may work with these document types, and nothing else:"]

	for row in settings.doctype_policies:
		verbs = [name for name, (field, _perm) in OPERATIONS.items() if row.get(field)]
		if not verbs:
			continue

		line = f"- {row.document_type}: {', '.join(verbs)}"
		if row.requires_approval:
			line += " (changes need the customer to confirm first)"
		if row.notes:
			line += f"\n  Note: {row.notes}"
		lines.append(line)

	return "\n".join(lines)


def describe_all_mode(settings) -> str:
	"""What the assistant can reach when nothing is listed one by one."""
	verbs = [
		name
		for name, (field, _perm) in OPERATIONS.items()
		if getattr(settings, f"all_{field}", 0)
	]

	lines = [
		"You can work with any document type in the system, as long as the user you act "
		f"for is allowed to. On anything not named below you may: {', '.join(verbs) or 'nothing'}.",
		"You do not know what document types exist. Use find_doctypes to look one up by name "
		"before using it, and describe_doctype to see its fields.",
	]

	if settings.all_requires_approval:
		lines.append("Changes to anything not named below need the customer to confirm first.")

	specific = [row for row in settings.doctype_policies if row.document_type]
	if specific:
		lines.append("\nThese have their own rules, which override the above:")
		for row in specific:
			allowed = [n for n, (f, _p) in OPERATIONS.items() if row.get(f)]
			line = f"- {row.document_type}: {', '.join(allowed) or 'nothing'}"
			if row.requires_approval:
				line += " (needs confirmation)"
			if row.notes:
				line += f"\n  Note: {row.notes}"
			lines.append(line)

	return "\n".join(lines)
