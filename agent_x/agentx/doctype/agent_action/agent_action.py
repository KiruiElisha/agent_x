"""A document change the assistant wants to make.

Every create, update, submit, cancel, and delete becomes one of these first.
Nothing is written until `execute()` runs, and `execute()` runs as the contact's
mapped user so Frappe's own permission checks still apply. The record is the
audit trail: what was asked, who it ran as, and what happened.
"""

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, now_datetime

WRITE_ACTIONS = ("create", "update", "submit", "cancel", "delete")


class AgentActionError(frappe.ValidationError):
	pass


class AgentAction(Document):
	def get_payload(self) -> dict:
		if not self.payload:
			return {}
		try:
			return json.loads(self.payload)
		except ValueError:
			frappe.throw(_("The stored payload for {0} is not valid JSON.").format(self.name))

	# ------------------------------------------------------------------ desk actions

	@frappe.whitelist()
	def approve(self) -> dict:
		"""Approve and run. Only someone who could do it themselves may approve."""
		if self.status not in ("Pending",):
			frappe.throw(_("Only a pending action can be approved. This one is {0}.").format(self.status))

		self.check_approver_permission()

		self.db_set(
			{"status": "Approved", "approved_by": frappe.session.user}, update_modified=False
		)
		return self.execute()

	@frappe.whitelist()
	def reject(self, reason: str | None = None) -> dict:
		if self.status != "Pending":
			frappe.throw(_("Only a pending action can be rejected."))

		self.check_approver_permission()

		self.db_set(
			{"status": "Rejected", "approved_by": frappe.session.user, "error": reason},
			update_modified=False,
		)
		frappe.db.commit()
		return {"status": "Rejected"}

	def check_approver_permission(self) -> None:
		"""An approver must hold the permission the action needs.

		Without this, anyone who can open the Agent Action list could approve a
		change they are not allowed to make themselves.
		"""
		permission = {"create": "create", "update": "write", "delete": "delete"}.get(
			self.action, "submit"
		)

		if not frappe.has_permission(self.document_type, permission, doc=self.document_name or None):
			frappe.throw(
				_("You need {0} permission on {1} to approve this.").format(
					permission, self.document_type
				),
				frappe.PermissionError,
			)

	# ------------------------------------------------------------------ execution

	def execute(self) -> dict:
		"""Carry out the change as the acting user."""
		if self.status not in ("Approved", "Pending"):
			frappe.throw(_("This action is {0} and cannot run.").format(self.status))

		settings = frappe.get_cached_doc("AgentX Settings")

		if settings.dry_run:
			self.db_set(
				{"status": "Executed", "executed_on": now_datetime(), "error": "Dry run: nothing was written."},
				update_modified=False,
			)
			frappe.db.commit()
			return {"status": "Executed", "dry_run": True}

		user = self.acting_user or settings.agent_user
		if not user:
			self.fail(_("No acting user is set, so there is no identity to run as."))
			frappe.throw(_("No acting user is set for this action."))

		try:
			# Run as the mapped user so Frappe applies their permissions,
			# user permissions, and document ownership as usual.
			with switch_user(user):
				result = self.run_action()

		except Exception as exc:
			frappe.db.rollback()
			self.fail(str(exc))
			raise

		self.db_set(
			{
				"status": "Executed",
				"executed_on": now_datetime(),
				"document_name": result.get("name") or self.document_name,
				"error": None,
			},
			update_modified=False,
		)
		frappe.db.commit()

		return {"status": "Executed", **result}

	def run_action(self) -> dict:
		payload = self.get_payload()

		if self.action == "create":
			return self.do_create(payload)
		if self.action == "update":
			return self.do_update(payload)
		if self.action == "submit":
			return self.do_submit()
		if self.action == "cancel":
			return self.do_cancel()
		if self.action == "delete":
			return self.do_delete()

		raise AgentActionError(_("Unknown action: {0}").format(self.action))

	def do_create(self, payload: dict) -> dict:
		doc = frappe.get_doc({"doctype": self.document_type, **payload})
		doc.insert()
		return {"name": doc.name, "docstatus": doc.docstatus}

	def do_update(self, payload: dict) -> dict:
		doc = frappe.get_doc(self.document_type, self.document_name)

		if doc.docstatus == 1:
			# A submitted document only accepts allow-on-submit fields, and
			# silently dropping the rest would look like the change worked.
			allowed = {f.fieldname for f in doc.meta.get("fields", {"allow_on_submit": 1})}
			blocked = set(payload) - allowed
			if blocked:
				raise AgentActionError(
					_("{0} is submitted, so these fields cannot be changed: {1}").format(
						self.document_name, ", ".join(sorted(blocked))
					)
				)

		for fieldname, value in payload.items():
			doc.set(fieldname, value)

		doc.save()
		return {"name": doc.name, "docstatus": doc.docstatus}

	def do_submit(self) -> dict:
		doc = frappe.get_doc(self.document_type, self.document_name)
		if doc.docstatus != 0:
			raise AgentActionError(
				_("{0} is already {1}.").format(
					self.document_name, "submitted" if doc.docstatus == 1 else "cancelled"
				)
			)
		doc.submit()
		return {"name": doc.name, "docstatus": doc.docstatus}

	def do_cancel(self) -> dict:
		doc = frappe.get_doc(self.document_type, self.document_name)
		if doc.docstatus != 1:
			raise AgentActionError(_("{0} is not submitted, so it cannot be cancelled.").format(self.document_name))
		doc.cancel()
		return {"name": doc.name, "docstatus": doc.docstatus}

	def do_delete(self) -> dict:
		frappe.delete_doc(self.document_type, self.document_name)
		return {"name": self.document_name, "deleted": True}

	def fail(self, message: str) -> None:
		self.db_set({"status": "Failed", "error": message[:500]}, update_modified=False)
		frappe.db.commit()


class switch_user:
	"""Run a block as another user, restoring the original afterwards.

	frappe.set_user swaps both the session user and the permission cache, so it
	has to be undone even when the block raises.
	"""

	def __init__(self, user: str):
		self.user = user
		self.previous = None

	def __enter__(self):
		self.previous = frappe.session.user
		frappe.set_user(self.user)
		return self

	def __exit__(self, *exc_info):
		frappe.set_user(self.previous)
		return False


def create_action(
	*,
	action: str,
	document_type: str,
	document_name: str | None,
	payload: dict | None,
	summary: str,
	acting_user: str,
	contact: str | None = None,
	conversation: str | None = None,
	agent_run: str | None = None,
	status: str = "Pending",
) -> Document:
	"""Record an intended change."""
	doc = frappe.get_doc(
		{
			"doctype": "Agent Action",
			"action": action,
			"document_type": document_type,
			"document_name": document_name,
			"payload": frappe.as_json(payload or {}),
			"summary": summary[:500],
			"acting_user": acting_user,
			"requested_by_contact": contact,
			"conversation": conversation,
			"agent_run": agent_run,
			"status": status,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def expire_pending() -> None:
	"""Time out confirmations nobody answered. Scheduled hourly."""
	settings = frappe.get_cached_doc("AgentX Settings")
	minutes = settings.confirm_timeout_minutes or 10
	cutoff = add_to_date(now_datetime(), minutes=-minutes)

	stale = frappe.get_all(
		"Agent Action", filters={"status": "Pending", "creation": ("<", cutoff)}, pluck="name"
	)
	if not stale:
		return

	for name in stale:
		frappe.db.set_value("Agent Action", name, "status", "Expired", update_modified=False)

	# Free any conversation that was blocked waiting on one of these.
	frappe.db.sql(
		"""
		UPDATE `tabAgent Conversation`
		SET status = 'Active', pending_action = NULL, pending_expires_on = NULL
		WHERE status = 'Awaiting Confirmation' AND pending_action IN %(names)s
		""",
		{"names": tuple(stale)},
	)
	frappe.db.commit()
