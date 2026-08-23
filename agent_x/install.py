"""First run setup."""

import frappe


def after_install() -> None:
	seed_settings()


def seed_settings() -> None:
	"""Fill in the defaults that only make sense once the site exists."""
	settings = frappe.get_single("AgentX Settings")

	if not settings.whatsapp_provider:
		# WaClient needs no extra infrastructure, so it is the default. Switch to
		# the self-hosted bridge only where a long-running process can live.
		settings.whatsapp_provider = "WaClient"

	if not settings.waclient_api_url:
		settings.waclient_api_url = "https://api.waclient.com"

	if not settings.bridge_url:
		settings.bridge_url = "http://127.0.0.1:8787"

	if not settings.ai_provider:
		settings.ai_provider = "Google Gemini"
		settings.ai_model = "gemini-2.5-flash"

	# Safe defaults: the assistant is off, and when it is switched on it asks
	# before it changes anything.
	settings.confirm_before_write = 1
	settings.require_user_mapping = 1
	settings.reply_scope = "Only Allowed Numbers"

	settings.flags.ignore_permissions = True
	settings.save()
	frappe.db.commit()
