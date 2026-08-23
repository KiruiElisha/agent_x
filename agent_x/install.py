"""First run setup."""

import frappe

# Defaults that only make sense once a site exists. Applied on install, and by
# a patch for sites that installed before a field was added.
DEFAULTS = {
	"whatsapp_provider": "WaClient",
	"waclient_api_url": "https://api.waclient.com",
	"bridge_url": "http://127.0.0.1:8787",
	"ai_provider": "Google Gemini",
	"ai_model": "gemini-2.5-flash",
	"policy_mode": "Listed Documents Only",
	"reply_scope": "Only Allowed Numbers",
	# Safe by default: the assistant asks before it changes anything, and only
	# acts for numbers explicitly mapped to a user.
	"confirm_before_write": 1,
	"require_user_mapping": 1,
	"confirm_timeout_minutes": 10,
	"max_actions_per_conversation": 20,
	# Behaviour that only costs something when the feature is switched on.
	"allow_document_pdfs": 1,
	"send_draft_after_lines": 5,
	"pdf_retention_hours": 24,
	"alerts_enabled": 1,
	"enable_catalogue": 1,
	"show_stock_levels": 1,
	"handoff_enabled": 1,
	"handoff_timeout_minutes": 60,
	"use_corrections": 1,
	"correction_limit": 20,
	"mark_messages_read": 1,
	"send_typing_indicator": 1,
	"transcribe_voice_notes": 1,
	"max_audio_mb": 8,
	"log_messages": 1,
	"log_retention_days": 90,
	"request_timeout": 30,
	"history_limit": 12,
	"max_tool_iterations": 6,
	"max_reply_characters": 1500,
	"ai_temperature": 0.3,
	"ai_max_output_tokens": 2048,
	"ai_read_images": 1,
	"ai_max_image_mb": 4,
	"retrieval_top_k": 4,
	"retrieval_min_score": 0.5,
	"chunk_size": 1200,
	"chunk_overlap": 150,
}


def after_install() -> None:
	seed_settings()


def seed_settings() -> None:
	"""Fill in any default that has not been set.

	Only writes empty fields, so it never overwrites a choice somebody made.
	Safe to run again, which is what lets the patch reuse it after new fields
	are added to an existing install.
	"""
	settings = frappe.get_single("AgentX Settings")
	changed = []

	for field, value in DEFAULTS.items():
		if not settings.meta.has_field(field):
			# The field belongs to a later version than this site has migrated to.
			continue

		current = settings.get(field)
		if current in (None, ""):
			settings.set(field, value)
			changed.append(field)

	if not changed:
		return

	settings.flags.ignore_permissions = True
	settings.flags.ignore_validate = True
	settings.save()
	frappe.db.commit()

	print(f"AgentX: set defaults for {len(changed)} settings")
