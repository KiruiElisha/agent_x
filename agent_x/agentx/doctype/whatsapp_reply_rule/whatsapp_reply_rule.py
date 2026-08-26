"""Answers that never need the model.

Every call to the model carries the whole system prompt and every tool
definition with it. Measured on this app that is roughly 3,500 tokens before
the customer's message is even counted, and a single message can take several
calls. A greeting, an opening-hours question, or a price-list request has the
same answer every time, and paying a model to reason it out is the fastest way
to burn a quota.

A rule that matches answers directly. Nothing reaches the model at all.
"""

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

CACHE_KEY = "agentx:reply_rules"

# Measured: system prompt plus tool definitions plus history, before the message.
AVERAGE_CALL_TOKENS = 3500


class WhatsAppReplyRule(Document):
	def validate(self) -> None:
		self.pattern = "\n".join(
			line.strip() for line in (self.pattern or "").splitlines() if line.strip()
		)
		if not self.pattern:
			frappe.throw(_("Give at least one thing to match on."))

		if self.match_type == "Regex":
			for line in self.patterns():
				try:
					re.compile(line)
				except re.error as exc:
					frappe.throw(_("{0} is not a valid pattern: {1}").format(line, exc))

		self.validate_template()

	def validate_template(self) -> None:
		"""Catch a broken template on save, not mid-conversation."""
		try:
			frappe.render_template(
				self.reply,
				{"contact": frappe._dict(contact_name="Test"), "settings": frappe._dict()},
			)
		except Exception as exc:
			frappe.throw(_("The reply is not a valid template: {0}").format(str(exc)[:200]))

	def patterns(self) -> list[str]:
		return [line.strip() for line in (self.pattern or "").splitlines() if line.strip()]

	def on_update(self) -> None:
		clear_cache()
		self.rebuild_vectors()

	def rebuild_vectors(self) -> None:
		"""Embed the phrasings, when this rule matches by meaning.

		Only on a real change, because each rebuild costs embedding calls.
		"""
		from agent_x.agent import semantic

		if self.match_type != "Semantic":
			if self.vectors:
				self.db_set({"vectors": None, "vectors_built_on": None}, update_modified=False)
				semantic.clear_matrix()
			return

		before = self.get_doc_before_save()
		unchanged = before and before.pattern == self.pattern and self.vectors
		if unchanged:
			return

		settings = frappe.get_cached_doc("AgentX Settings")
		try:
			blob = semantic.build_rule_vectors(self, settings)
		except Exception as exc:
			# The rule still saves; it just will not match until embeddings work.
			frappe.msgprint(
				_("Saved, but the phrasings could not be embedded yet: {0}").format(str(exc)[:200]),
				title=_("Semantic matching is not ready"),
				indicator="orange",
			)
			return

		self.db_set(
			{"vectors": blob, "vectors_built_on": now_datetime()}, update_modified=False
		)
		semantic.clear_matrix()

	def on_trash(self) -> None:
		from agent_x.agent import semantic

		clear_cache()
		semantic.clear_matrix()

	def matches(self, text: str) -> bool:
		body = (text or "") if self.case_sensitive else (text or "").lower()

		for raw in self.patterns():
			needle = raw if self.case_sensitive else raw.lower()

			if self.match_type == "Exact" and body.strip() == needle.strip():
				return True
			if self.match_type == "Contains" and needle in body:
				return True
			if self.match_type == "Starts With" and body.strip().startswith(needle.strip()):
				return True
			if self.match_type == "Regex":
				flags = 0 if self.case_sensitive else re.IGNORECASE
				try:
					if re.search(raw, text or "", flags):
						return True
				except re.error:
					continue

		return False

	def render(self, contact, settings) -> str:
		try:
			return frappe.render_template(
				self.reply, {"contact": contact, "settings": settings}
			).strip()
		except Exception:
			# A broken template should still answer with something.
			frappe.log_error(frappe.get_traceback(), f"AgentX: reply rule template failed ({self.name})")
			return (self.reply or "").strip()

	def record_use(self) -> None:
		self.db_set(
			{
				"times_used": (self.times_used or 0) + 1,
				"last_used_on": now_datetime(),
				"tokens_saved": (self.tokens_saved or 0) + AVERAGE_CALL_TOKENS,
			},
			update_modified=False,
		)

	@frappe.whitelist()
	def test_match(self, text: str) -> dict:
		"""Try a message against this rule without sending anything."""
		hit = self.matches(text or "")
		return {
			"matched": hit,
			"reply": self.render(frappe._dict(contact_name="Test"), frappe.get_cached_doc("AgentX Settings"))
			if hit
			else None,
		}


def clear_cache() -> None:
	try:
		frappe.cache.delete_value(CACHE_KEY)
	except Exception:
		pass

	if hasattr(frappe.local, "agentx_reply_rules"):
		del frappe.local.agentx_reply_rules


def active_rules() -> list[str]:
	"""Enabled rule names, highest priority first.

	Cached twice, like the alert dispatcher: this runs on every inbound message
	and must not cost a query to discover there are no rules.
	"""
	local = getattr(frappe.local, "agentx_reply_rules", None)
	if local is not None:
		return local

	names = None
	try:
		cached = frappe.cache.get_value(CACHE_KEY)
		if cached is not None:
			names = cached
	except Exception:
		pass

	if names is None:
		try:
			names = frappe.get_all(
				"WhatsApp Reply Rule",
				filters={"enabled": 1},
				order_by="priority desc, creation asc",
				pluck="name",
			)
		except Exception:
			# The doctype may not exist yet, during install or before migrate.
			names = []

		try:
			frappe.cache.set_value(CACHE_KEY, names, expires_in_sec=3600)
		except Exception:
			pass

	frappe.local.agentx_reply_rules = names
	return names


def find_match(text: str, settings=None):
	"""The first rule that answers this message, or None.

	Word matching runs first because it is free. Semantic matching costs one
	embedding, so it is only reached when nothing cheaper answered.
	"""
	body = (text or "").strip()
	if not body:
		return None

	semantic_rules = []

	for name in active_rules():
		try:
			rule = frappe.get_cached_doc("WhatsApp Reply Rule", name)
		except Exception:
			continue

		if not rule.enabled:
			continue

		if rule.match_type == "Semantic":
			semantic_rules.append(rule)
			continue

		if rule.matches(body):
			return rule

	if not semantic_rules:
		return None

	from agent_x.agent import semantic as semantic_matching

	settings = settings or frappe.get_cached_doc("AgentX Settings")
	name, score = semantic_matching.best_rule(body, settings)
	if not name:
		return None

	rule = frappe.get_cached_doc("WhatsApp Reply Rule", name)
	rule.flags.match_score = score
	return rule
