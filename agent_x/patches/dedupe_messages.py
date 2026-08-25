"""Remove duplicate message rows before the unique index goes on.

Providers send more than one event for a single message: WaClient emits both a
`chats.update` and a `messages.upsert`. The old exists() check was not atomic,
so two webhooks arriving together could both pass it and both insert, which
also meant the assistant answered the same message twice.
"""

import frappe


def execute() -> None:
	if not frappe.db.table_exists("WhatsApp Message"):
		return

	# Keep the oldest row for each provider id; it is the one anything else links to.
	duplicates = frappe.db.sql(
		"""
		SELECT message_id, COUNT(*) AS n, MIN(creation) AS first_seen
		FROM `tabWhatsApp Message`
		WHERE message_id IS NOT NULL AND message_id != ''
		GROUP BY message_id
		HAVING n > 1
		""",
		as_dict=True,
	)

	removed = 0
	for row in duplicates:
		extra = frappe.db.sql(
			"""
			SELECT name FROM `tabWhatsApp Message`
			WHERE message_id = %(id)s AND creation > %(first)s
			""",
			{"id": row.message_id, "first": row.first_seen},
			as_dict=True,
		)
		for doc in extra:
			frappe.db.delete("WhatsApp Message", {"name": doc.name})
			removed += 1

	if removed:
		frappe.db.commit()
		print(f"AgentX: removed {removed} duplicate message rows")


	enforce_unique_index()


def enforce_unique_index() -> None:
	"""Convert the message_id index to a unique one.

	Frappe creates a plain index for a search_index field and will not upgrade
	an existing index in place, so the unique flag alone changes nothing. The
	index is what actually prevents a double insert when two webhooks for the
	same message arrive at once; the exists() check in the webhook is only the
	cheap path in front of it.
	"""
	table = "tabWhatsApp Message"

	existing = frappe.db.sql(f"SHOW INDEX FROM `{table}`", as_dict=True)
	current = [i for i in existing if i["Column_name"] == "message_id"]

	if any(i["Non_unique"] == 0 for i in current):
		return

	for index in current:
		try:
			frappe.db.sql_ddl(f"ALTER TABLE `{table}` DROP INDEX `{index['Key_name']}`")
		except Exception:
			frappe.log_error(frappe.get_traceback(), "AgentX: could not drop the message_id index")
			return

	try:
		frappe.db.sql_ddl(
			f"ALTER TABLE `{table}` ADD UNIQUE INDEX `message_id` (`message_id`)"
		)
		print("AgentX: message_id is now unique")
	except Exception:
		# Leaving it non-unique is survivable; the webhook still dedupes.
		frappe.log_error(frappe.get_traceback(), "AgentX: could not make message_id unique")
