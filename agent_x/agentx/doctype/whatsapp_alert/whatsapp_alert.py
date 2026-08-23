"""One rule for a message the system sends on its own."""

import frappe
from frappe import _
from frappe.model.document import Document

SCHEDULED = ("Days Before", "Days After")


class WhatsAppAlert(Document):
	def validate(self) -> None:
		self.validate_event()
		self.validate_recipient()
		self.validate_template()

	def validate_event(self) -> None:
		meta = frappe.get_meta(self.document_type)

		if self.event in ("On Submit", "On Cancel") and not meta.is_submittable:
			frappe.throw(
				_("{0} is not submittable, so it never fires this event.").format(
					frappe.bold(self.document_type)
				)
			)

		if self.event == "On Value Change":
			if not self.value_change_field:
				frappe.throw(_("Name the field to watch."))
			if not meta.get_field(self.value_change_field):
				frappe.throw(
					_("{0} has no field called {1}.").format(
						self.document_type, frappe.bold(self.value_change_field)
					)
				)

		if self.event in SCHEDULED:
			if not self.date_field:
				frappe.throw(_("Name the date field to count from."))

			field = meta.get_field(self.date_field)
			if not field or field.fieldtype not in ("Date", "Datetime"):
				frappe.throw(
					_("{0} is not a Date or Datetime field on {1}.").format(
						frappe.bold(self.date_field), self.document_type
					)
				)

	def validate_recipient(self) -> None:
		if self.recipient_type == "Fixed Number":
			from agent_x.core.phone import digits_only

			cleaned = digits_only(self.fixed_number)
			if not cleaned:
				frappe.throw(_("{0} is not a usable phone number.").format(self.fixed_number))
			self.fixed_number = cleaned

		elif self.recipient_type == "Field on Document":
			if not self.recipient_field:
				frappe.throw(_("Name the field holding the phone number."))

			# A dotted path reads through a link, so only check the first hop.
			first = self.recipient_field.split(".")[0]
			if not frappe.get_meta(self.document_type).get_field(first):
				frappe.throw(
					_("{0} has no field called {1}.").format(self.document_type, frappe.bold(first))
				)

	def validate_template(self) -> None:
		"""Catch a broken template here rather than at three in the morning."""
		try:
			frappe.render_template(self.message, {"doc": frappe._dict(name="TEST"), "alert": self})
		except Exception as exc:
			frappe.throw(_("The message template is not valid: {0}").format(str(exc)[:300]))

	def on_update(self) -> None:
		from agent_x.core.alerts import clear_cache

		clear_cache()

	def on_trash(self) -> None:
		from agent_x.core.alerts import clear_cache

		clear_cache()

	@frappe.whitelist()
	def preview(self, docname: str) -> dict:
		"""Render this alert against a real document, without sending anything."""
		from agent_x.core import alerts

		if not frappe.db.exists(self.document_type, docname):
			frappe.throw(_("There is no {0} called {1}.").format(self.document_type, docname))

		doc = frappe.get_doc(self.document_type, docname)

		try:
			message = frappe.render_template(self.message, {"doc": doc, "alert": self})
		except Exception as exc:
			return {"ok": False, "error": _("Template failed: {0}").format(str(exc)[:300])}

		return {
			"ok": True,
			"message": message,
			"number": alerts.resolve_number(self, doc),
			"passes_condition": alerts.passes_condition(self, doc),
			"already_sent": alerts.already_sent(self.name, self.document_type, docname),
		}

	@frappe.whitelist()
	def send_now(self, docname: str) -> dict:
		"""Send this alert for one document, for testing."""
		frappe.only_for("System Manager")

		from agent_x.core import alerts

		result = alerts.send(self.name, self.document_type, docname)
		return result or {"sent": False, "reason": _("Nothing was sent. Check the alert's last error.")}
