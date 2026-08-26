"""Create the rules that remove the most model calls.

Greetings and thanks are the two most common messages a business number gets,
they always get the same answer, and each one otherwise costs a full model call
carrying the entire prompt. Seeded disabled where the wording has to be the
business's own, so nothing goes out sounding generic without being read first.
"""

import frappe

RULES = [
	{
		"title": "Greeting",
		"match_type": "Exact",
		"pattern": "hi\nhii\nhey\nhello\nhallo\nhi there\ngood morning\ngood afternoon\ngood evening\nniaje\nsasa\nmambo\nhabari",
		"reply": "Hello! How can I help you today?",
		"priority": 10,
		"enabled": 1,
	},
	{
		"title": "Thanks",
		"match_type": "Exact",
		"pattern": "thanks\nthank you\nthx\nasante\nasante sana\nok thanks\nthanks alot\nthanks a lot",
		"reply": "You are welcome. Let me know if you need anything else.",
		"priority": 10,
		"enabled": 1,
	},
	{
		"title": "Opening Hours",
		"match_type": "Contains",
		"pattern": "opening hours\nwhat time do you open\nwhat time do you close\nare you open\nyour hours",
		"reply": "We are open Monday to Friday, 8am to 5pm.",
		"priority": 5,
		# Disabled: the hours have to be the real ones before this goes out.
		"enabled": 0,
	},
	{
		"title": "Where Are You",
		"match_type": "Contains",
		"pattern": "where are you located\nyour location\nwhere is your shop\nyour address\ndirections",
		"reply": "Please set your address here before enabling this rule.",
		"priority": 5,
		"enabled": 0,
	},
]


def execute() -> None:
	if not frappe.db.exists("DocType", "WhatsApp Reply Rule"):
		return

	created = []
	for rule in RULES:
		if frappe.db.exists("WhatsApp Reply Rule", rule["title"]):
			continue

		try:
			frappe.get_doc({"doctype": "WhatsApp Reply Rule", **rule}).insert(ignore_permissions=True)
			created.append(rule["title"])
		except Exception:
			frappe.log_error(frappe.get_traceback(), "AgentX: could not seed a reply rule")

	if created:
		frappe.db.commit()
		print(f"AgentX: added starter reply rules: {', '.join(created)}")
