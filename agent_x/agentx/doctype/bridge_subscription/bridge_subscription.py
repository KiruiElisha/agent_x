import frappe
from frappe.model.document import Document


class BridgeSubscription(Document):
	def validate(self) -> None:
		self.email = (self.email or "").strip().lower()
