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
	used = frappe.db.count(
		"Agent Action",
		{"document_type": doctype, "status": "Executed", "creation": (">", since)},
	)

	if used >= cap:
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
	if not settings.automation_enabled or not settings.doctype_policies:
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
