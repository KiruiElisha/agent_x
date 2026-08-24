"""Repair sidebar rows that have no link_type.

An earlier version of this app shipped sidebar group rows without a link_type.
Frappe lowercases that field while building the boot payload without checking
it is set, so a single null row raises AttributeError and the whole desk fails
to load with SessionBootFailed.

Re-syncing the fixture replaces those rows, but this runs first and repairs
them directly, so a site is never left one failed sync away from an unusable
desk.
"""

import frappe


def execute() -> None:
	if not frappe.db.table_exists("Workspace Sidebar Item"):
		return

	broken = frappe.db.sql(
		"""
		SELECT child.name
		FROM `tabWorkspace Sidebar Item` child
		INNER JOIN `tabWorkspace Sidebar` parent ON parent.name = child.parent
		WHERE parent.app = 'agent_x' AND (child.link_type IS NULL OR child.link_type = '')
		""",
		as_dict=True,
	)

	if not broken:
		return

	for row in broken:
		frappe.db.set_value(
			"Workspace Sidebar Item", row.name, "link_type", "DocType", update_modified=False
		)

	frappe.db.commit()
	frappe.clear_cache()

	print(f"AgentX: repaired {len(broken)} sidebar rows that had no link_type")
