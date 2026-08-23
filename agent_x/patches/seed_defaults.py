"""Fill in defaults for settings added after a site was already installed.

after_install only runs once, so a field introduced later sits empty on an
existing site. The form then shows blank selects even though the code falls
back sensibly, which reads as broken.
"""

import frappe


def execute() -> None:
	if not frappe.db.exists("DocType", "AgentX Settings"):
		return

	from agent_x.install import seed_settings

	seed_settings()
