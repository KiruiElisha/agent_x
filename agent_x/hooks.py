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

# Scheduled Tasks
# ---------------
scheduler_events = {
	"hourly": [
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
