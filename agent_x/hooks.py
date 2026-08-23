app_name = "agent_x"
app_title = "AgentX"
app_publisher = "Rono"
app_description = "ERPNext automation agent, with WhatsApp integration"
app_email = "ronoelisha625@gmail.com"
app_license = "mit"

add_to_apps_screen = [
	{
		"name": "agent_x",
		"logo": "/assets/agent_x/images/agentx-logo.svg",
		"title": "AgentX",
		"route": "/app/agentx",
	}
]

# Installation
# ------------
after_install = "agent_x.install.after_install"

# Document Events
# ---------------
# Alerts hang off ordinary document events. The dispatcher checks one cached
# set and returns for any doctype without an alert, so the cost on an unrelated
# save is a single Redis read.

doc_events = {
	"*": {
		"after_insert": "agent_x.core.alerts.handle",
		"on_submit": "agent_x.core.alerts.handle",
		"on_cancel": "agent_x.core.alerts.handle",
		"on_update": "agent_x.core.alerts.handle",
	}
}

# Scheduled Tasks
# ---------------
scheduler_events = {
	"hourly": [
		# Date based reminders, and anything held back for business hours.
		"agent_x.core.alerts.run_scheduled",
		# Give a conversation back if nobody picked it up.
		"agent_x.agent.handoff.release_expired",
		# Confirmations nobody answered must not sit pending forever.
		"agent_x.agentx.doctype.agent_action.agent_action.expire_pending",
		# Generated PDFs are public while a hosted provider fetches them.
		"agent_x.core.printing.cleanup_public_pdfs",
	],
	"daily": [
		# Re-index anything whose source text changed.
		"agent_x.agent.knowledge.rebuild_stale",
		"agent_x.agentx.doctype.whatsapp_message.whatsapp_message.delete_old_logs",
	],
}

# Automatically update python controller files with type annotations for this app.
export_python_type_annotations = True

require_type_annotated_api_methods = True
