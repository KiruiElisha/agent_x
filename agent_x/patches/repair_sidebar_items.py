"""Repair sidebar rows shipped by an earlier version of this app.

Two faults, both from using `Sidebar Item Group` for section headings:

  link_type was null. Frappe lowercases that field while building the boot
  payload without checking it is set, so one null row raised AttributeError and
  the whole desk failed with SessionBootFailed.

  The type itself. The sidebar only exempts `Section Break` from needing a
  route; any other non-Link type renders as a link with no path, so clicking a
  heading asked for a page named after its label and got a 404.

Re-syncing the fixture replaces these rows, but this runs first so a site is
never one failed sync away from an unusable desk.
"""

import frappe


def execute() -> None:
	if not frappe.db.table_exists("Workspace Sidebar Item"):
		return

	rows = frappe.db.sql(
		"""
		SELECT child.name, child.type, child.link_type
		FROM `tabWorkspace Sidebar Item` child
		INNER JOIN `tabWorkspace Sidebar` parent ON parent.name = child.parent
		WHERE parent.app = 'agent_x'
		""",
		as_dict=True,
	)

	repaired = 0

	for row in rows:
		values = {}

		# Section Break is the only heading type the sidebar knows how to render
		# without a route.
		if row.type and row.type != "Section Break" and row.type != "Link":
			values["type"] = "Section Break"

		if not row.link_type:
			values["link_type"] = "DocType"

		if values:
			frappe.db.set_value("Workspace Sidebar Item", row.name, values, update_modified=False)
			repaired += 1

	if not repaired:
		return

	frappe.db.commit()
	frappe.clear_cache()

	print(f"AgentX: repaired {repaired} sidebar rows")
