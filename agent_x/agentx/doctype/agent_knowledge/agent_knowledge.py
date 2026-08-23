"""One source of background knowledge the assistant can look things up in."""

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document


class AgentKnowledge(Document):
	def validate(self) -> None:
		if self.source_type == "Document" and not (self.reference_doctype and self.reference_name):
			frappe.throw(_("Pick the document to read."))

		if self.source_type == "File" and not self.attachment:
			frappe.throw(_("Attach a plain text or markdown file."))

		if self.source_type == "Text" and not (self.content or "").strip():
			frappe.throw(_("Add some content to index."))

	def on_update(self) -> None:
		"""Re-index when the source actually changed, not on every save."""
		from agent_x.agent import knowledge

		if not self.enabled:
			return

		try:
			current = hashlib.sha256(knowledge.content_of(self).encode()).hexdigest()
		except Exception:
			return

		if current != (self.content_hash or ""):
			knowledge.enqueue_build(self.name)

	def on_trash(self) -> None:
		from agent_x.agent import knowledge

		frappe.db.delete("Agent Knowledge Chunk", {"knowledge": self.name})
		knowledge.bump_version()

	@frappe.whitelist()
	def rebuild(self) -> dict:
		"""Index this source now, rather than waiting for the queue."""
		from agent_x.agent import knowledge

		return knowledge.build(self.name)

	@frappe.whitelist()
	def preview_search(self, query: str) -> dict:
		"""Show what a question would retrieve, for tuning the content."""
		from agent_x.agent import knowledge

		settings = frappe.get_cached_doc("AgentX Settings")
		return {"hits": knowledge.search(query, settings)}
