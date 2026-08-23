"""Turn on the feature switches added after a site was already installed.

The first defaults patch only filled fields that were empty. A Check field
added later reads as 0, not empty, so it was skipped and every new feature
arrived switched off — which looks like the feature is broken rather than idle.

This runs once per site and applies the doctype's own default to those
switches. It is safe because these fields did not exist when the site was
installed, so a 0 in them is an absence rather than somebody's decision.
"""

import frappe

# Only switches introduced after the first release. Anything a user could
# already have deliberately turned off stays out of this list.
SWITCHES = (
	"alerts_enabled",
	"enable_catalogue",
	"show_stock_levels",
	"allow_document_pdfs",
	"handoff_enabled",
	"use_corrections",
	"mark_messages_read",
	"send_typing_indicator",
	"transcribe_voice_notes",
)


def execute() -> None:
	if not frappe.db.exists("DocType", "AgentX Settings"):
		return

	settings = frappe.get_single("AgentX Settings")
	meta = settings.meta
	changed = []

	for field in SWITCHES:
		docfield = meta.get_field(field)
		if not docfield:
			continue

		if not settings.get(field):
			settings.set(field, int(docfield.default or 0))
			changed.append(field)

	# Numbers that came in as 0 for the same reason.
	for field, value in (
		("correction_limit", 20),
		("handoff_timeout_minutes", 60),
		("max_audio_mb", 8),
		("pdf_retention_hours", 24),
		("send_draft_after_lines", 5),
		("retrieval_top_k", 4),
		("chunk_size", 1200),
		("chunk_overlap", 150),
	):
		if meta.get_field(field) and not settings.get(field):
			settings.set(field, value)
			changed.append(field)

	if not changed:
		return

	settings.flags.ignore_permissions = True
	settings.flags.ignore_validate = True
	settings.save()
	frappe.db.commit()

	print(f"AgentX: enabled {len(changed)} settings added since install")
