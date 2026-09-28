"""Settings the production hardening depends on, for sites installed before it.

The inbound endpoint now refuses every event when no Webhook Token is set,
where it used to accept anything. A site that never set one gets one here, and
has to press Register Webhook once so WaClient starts sending it.

The per-contact rate limit is a new field. A Single reads a missing value as 0,
which means no limit, so it is seeded with the doctype default unless someone
has already chosen a value.

Sending now needs the AgentX Sender role (System Managers keep it), so the
role is created here too.
"""

import frappe


def execute() -> None:
	if not frappe.db.exists("DocType", "AgentX Settings"):
		return

	from agent_x.install import ensure_sender_role

	ensure_sender_role()

	settings = frappe.get_single("AgentX Settings")
	changed = []

	if settings.meta.get_field("max_messages_per_hour") and not frappe.db.exists(
		"Singles", {"doctype": "AgentX Settings", "field": "max_messages_per_hour"}
	):
		settings.max_messages_per_hour = 30
		changed.append("max_messages_per_hour")

	if not settings.get_password("webhook_token", raise_exception=False):
		settings.webhook_token = frappe.generate_hash(length=40)
		changed.append("webhook_token")

	if not changed:
		return

	settings.flags.ignore_permissions = True
	settings.flags.ignore_validate = True
	settings.save()
	frappe.db.commit()

	print("AgentX: set " + ", ".join(changed))
	if "webhook_token" in changed and (settings.whatsapp_provider or "WaClient") == "WaClient":
		print(
			"AgentX: a Webhook Token was generated. Open AgentX Settings and press "
			"Register Webhook, or inbound messages will be refused."
		)
