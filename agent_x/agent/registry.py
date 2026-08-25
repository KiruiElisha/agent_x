"""The tool catalogue handed to the model.

Only tools the current policy actually permits are advertised. A model that is
never told about `delete_document` cannot try to call it, which is a cheaper
defence than refusing the call afterwards.
"""

import json

import frappe
from frappe import _

from agent_x.agent import policy
from agent_x.agent.tools import catalogue, customers, documents, memory

# Operation each tool needs, used to decide whether to advertise it at all.
def handover_tool(ctx, reason: str | None = None) -> dict:
	"""Acknowledge the handover the runtime already performed.

	The runtime does the actual handing over, because it owns the conversation.
	This exists so the model gets a normal tool result back and stops trying.
	"""
	return {
		"handed_over": True,
		"note": _("A person has been notified. Tell the customer briefly and stop."),
	}


TOOL_OPERATIONS = {
	"list_documents": "read",
	"get_document": "read",
	"count_documents": "read",
	"describe_doctype": "read",
	"create_document": "create",
	"update_document": "update",
	"submit_document": "submit",
	"cancel_document": "cancel",
	"delete_document": "delete",
	"send_document": "read",
	"find_items": "read",
	"get_item": "read",
	"list_item_groups": "read",
	"find_doctypes": "read",
	"hand_over": None,
	"remember": None,
	"forget": None,
	"match_items": "read",
	"find_customer": "read",
	"link_customer": "read",
	"create_customer": "create",
}

HANDLERS = {
	"list_documents": documents.list_documents,
	"get_document": documents.get_document,
	"count_documents": documents.count_documents,
	"describe_doctype": documents.describe_doctype,
	"create_document": documents.create_document,
	"update_document": documents.update_document,
	"submit_document": documents.submit_document,
	"cancel_document": documents.cancel_document,
	"delete_document": documents.delete_document,
	"send_document": documents.send_document,
	"find_items": catalogue.find_items,
	"get_item": catalogue.get_item,
	"list_item_groups": catalogue.list_item_groups,
	"find_doctypes": documents.find_doctypes,
	"hand_over": handover_tool,
	"remember": memory.remember,
	"forget": memory.forget,
	"match_items": catalogue.match_items,
	"find_customer": customers.find_customer,
	"link_customer": customers.link_customer,
	"create_customer": customers.create_customer,
}


def doctype_property(doctypes: list[str], open_ended: bool = False) -> dict:
	"""Constrain the doctype argument to what is actually permitted.

	An enum is the strongest guard available, since the model cannot name what
	it was never shown. All Documents mode has no list to enumerate, so the
	argument opens up and the policy gate does the refusing instead.
	"""
	if open_ended:
		return {
			"type": "string",
			"description": (
				"The exact document type name, e.g. 'Sales Order'. "
				"Use find_doctypes first if you are not sure it exists."
			),
		}

	return {
		"type": "string",
		"description": "The document type to work with.",
		"enum": doctypes,
	}


def build_schemas(settings) -> list[dict]:
	"""JSON Schema definitions for every tool this configuration allows."""
	if not settings.automation_enabled:
		return []

	open_ended = (settings.policy_mode or "Listed Documents Only") == "All Documents"
	permitted = permitted_operations(settings)

	readable = sorted(permitted.get("read", set()))
	if not (readable or open_ended):
		return []

	dt = doctype_property(readable, open_ended)
	schemas: list[dict] = [
		{
			"name": "list_documents",
			"description": (
				"Find documents of a type, optionally filtered. Use this to look something up "
				"before answering, and always before changing a document, so you have its name."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"doctype": dt,
					"filters": {
						"type": "string",
						"description": (
							'A JSON object of field to value, e.g. {"status": "Draft"}. For anything '
							'other than equality use a two item list: {"grand_total": [">", 1000]}.'
						),
					},
					"fields": {
						"type": "array",
						"items": {"type": "string"},
						"description": "Field names to return. Leave empty for the usual list view fields.",
					},
					"limit": {"type": "integer", "description": "How many rows, at most 20."},
					"order_by": {"type": "string", "description": 'e.g. "creation desc".'},
				},
				"required": ["doctype"],
			},
		},
		{
			"name": "get_document",
			"description": "Read one document in full, by its exact name.",
			"parameters": {
				"type": "object",
				"properties": {
					"doctype": dt,
					"name": {"type": "string", "description": "The document's exact name or ID."},
					"fields": {
						"type": "array",
						"items": {"type": "string"},
						"description": "Only these fields. Leave empty for all readable fields.",
					},
				},
				"required": ["doctype", "name"],
			},
		},
		{
			"name": "count_documents",
			"description": "Count documents matching filters, without fetching them.",
			"parameters": {
				"type": "object",
				"properties": {
					"doctype": dt,
					"filters": {"type": "string", "description": "A JSON object of field to value."},
				},
				"required": ["doctype"],
			},
		},
		{
			"name": "describe_doctype",
			"description": (
				"List the fields of a document type, which ones are required, and what links "
				"they expect. Call this before creating a document type you have not created before."
			),
			"parameters": {
				"type": "object",
				"properties": {"doctype": dt},
				"required": ["doctype"],
			},
		},
	]

	if catalogue_enabled(settings, permitted):
		schemas.extend(catalogue_schemas())

	# Memory is always available. Without it a detour loses the request that
	# started the conversation.
	schemas.append(
		{
			"name": "remember",
			"description": (
				"Write down what the customer asked for that you have not done yet. Use it the "
				"moment something blocks you, before you go and deal with the blocker. Include "
				"the details you will need: item codes, quantities, what they wanted. It stays "
				"in front of you on every reply until you call forget."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"note": {
						"type": "string",
						"description": "What is still owed, in enough detail to act on later.",
					}
				},
				"required": ["note"],
			},
		}
	)
	schemas.append(
		{
			"name": "forget",
			"description": "Clear the note, once the work it describes is finished or dropped.",
			"parameters": {"type": "object", "properties": {}},
		}
	)

	if settings.handoff_enabled:
		schemas.append(
			{
				"name": "hand_over",
				"description": (
					"Hand this conversation to a person. Use it when the customer is upset, "
					"wants a refund or to complain, asks for a human, or needs something you "
					"have no tool for. After calling it, say briefly that someone will reply "
					"and stop."
				),
				"parameters": {
					"type": "object",
					"properties": {
						"reason": {
							"type": "string",
							"description": "One line telling the person taking over what is needed.",
						}
					},
					"required": ["reason"],
				},
			}
		)

	if catalogue_enabled(settings, permitted):
		schemas.append(
			{
				"name": "match_items",
				"description": (
					"Turn a customer's wording into real item codes. Give it the lines you read "
					"from their message, photo, or document. Never write an item code you did "
					"not get from here or from find_items."
				),
				"parameters": {
					"type": "object",
					"properties": {
						"requests": {
							"type": "string",
							"description": (
								'A JSON list of what they asked for, e.g. '
								'[{"name": "andolex rinse 200ml", "qty": 5}, {"name": "panadol", "qty": 2}]'
							),
						}
					},
					"required": ["requests"],
				},
			}
		)

	if customers_enabled(settings, permitted):
		schemas.extend(customer_schemas(permitted))

	if settings.allow_document_pdfs:
		schemas.append(
			{
				"name": "send_document",
				"description": (
					"Send the person a PDF of a document, using the print format this site "
					"normally uses for it. Use this when they ask for a copy, a receipt, an "
					"invoice, or confirmation of something you created. Find the document first "
					"so you have its exact name."
				),
				"parameters": {
					"type": "object",
					"properties": {
						"doctype": dt,
						"name": {"type": "string", "description": "The document's exact name."},
						"message": {
							"type": "string",
							"description": "Short caption to send with the file.",
						},
					},
					"required": ["doctype", "name"],
				},
			}
		)

	if open_ended:
		schemas.append(
			{
				"name": "find_doctypes",
				"description": (
					"Look up document types by name. You are not given a fixed list, so use "
					"this to find the exact name before reading or changing anything."
				),
				"parameters": {
					"type": "object",
					"properties": {
						"query": {"type": "string", "description": "Part of the name, e.g. 'invoice'."},
						"limit": {"type": "integer", "description": "How many, at most 25."},
					},
					"required": ["query"],
				},
			}
		)

	if permitted.get("create") or (open_ended and settings.all_can_create):
		schemas.append(
			{
				"name": "create_document",
				"description": (
					"Create a new document. Check the required fields with describe_doctype first. "
					"Give a short human summary of what you are creating and why."
				),
				"parameters": {
					"type": "object",
					"properties": {
						"doctype": doctype_property(sorted(permitted.get("create", [])), open_ended),
						"values": {
							"type": "string",
							"description": (
								"A JSON object of field name to value for the new document, "
								'e.g. {"lead_name": "Jane Doe", "company_name": "Acme"}.'
							),
						},
						"summary": {
							"type": "string",
							"description": "One line a person would understand, e.g. 'New lead for Jane Doe'.",
						},
					},
					"required": ["doctype", "values", "summary"],
				},
			}
		)

	if permitted.get("update") or (open_ended and settings.all_can_write):
		schemas.append(
			{
				"name": "update_document",
				"description": "Change fields on an existing document. Find its exact name first.",
				"parameters": {
					"type": "object",
					"properties": {
						"doctype": doctype_property(sorted(permitted.get("update", [])), open_ended),
						"name": {"type": "string", "description": "The document's exact name."},
						"values": {
							"type": "string",
							"description": 'A JSON object of field name to new value, e.g. {"status": "Open"}.',
						},
						"summary": {"type": "string", "description": "One line describing the change."},
					},
					"required": ["doctype", "name", "values", "summary"],
				},
			}
		)

	for operation, verb, note in (
		("submit", "submit_document", "Submit a draft document, making it final."),
		("cancel", "cancel_document", "Cancel a submitted document."),
		("delete", "delete_document", "Delete a document permanently. Prefer cancelling instead."),
	):
		enabled_by_default = open_ended and getattr(settings, f"all_can_{operation}", 0)
		if not (permitted.get(operation) or enabled_by_default):
			continue

		schemas.append(
			{
				"name": verb,
				"description": note,
				"parameters": {
					"type": "object",
					"properties": {
						"doctype": doctype_property(sorted(permitted.get(operation, [])), open_ended),
						"name": {"type": "string", "description": "The document's exact name."},
						"summary": {"type": "string", "description": "One line describing what this does."},
					},
					"required": ["doctype", "name", "summary"],
				},
			}
		)

	return schemas


def catalogue_enabled(settings, permitted: dict) -> bool:
	"""The catalogue reads Items, so it needs the Item policy like anything else."""
	return bool(
		settings.enable_catalogue
		and "Item" in permitted.get("read", set())
		and catalogue.available()
	)


def catalogue_schemas() -> list[dict]:
	"""Browsing what is for sale.

	Separate from list_documents because a customer wants a name, a price, and
	whether it is in stock, which live in three different doctypes.
	"""
	return [
		{
			"name": "find_items",
			"description": (
				"Search what the business sells. Returns the exact item code, the price, and "
				"how many are in stock. Always use this before putting anything on an order, "
				"so the item code is real and the price is current. Never quote a price you "
				"did not get from here."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"query": {
						"type": "string",
						"description": "What the customer called it. Leave empty to list everything.",
					},
					"item_group": {"type": "string", "description": "Narrow to one group."},
					"limit": {"type": "integer", "description": "How many to return, at most 25."},
					"in_stock_only": {
						"type": "boolean",
						"description": "Only items with stock available.",
					},
				},
			},
		},
		{
			"name": "get_item",
			"description": "One item in full: its price, unit, and stock. Use it to confirm a choice.",
			"parameters": {
				"type": "object",
				"properties": {"item_code": {"type": "string", "description": "The exact item code."}},
				"required": ["item_code"],
			},
		},
		{
			"name": "list_item_groups",
			"description": (
				"The kinds of thing sold, with how many items each has. Use this when the "
				"customer does not know what to ask for."
			),
			"parameters": {"type": "object", "properties": {}},
		},
	]


def customers_enabled(settings, permitted: dict) -> bool:
	return "Customer" in permitted.get("read", set()) and bool(
		frappe.db.exists("DocType", "Customer")
	)


def customer_schemas(permitted: dict) -> list[dict]:
	"""Working out who the conversation is for.

	Separate from the generic document tools because getting this wrong bills
	the wrong company, so the search is deliberate rather than a plain lookup.
	"""
	schemas = [
		{
			"name": "find_customer",
			"description": (
				"Work out which customer this conversation is for. Call it with no query "
				"first: it checks whether this number is already linked, then searches every "
				"place a phone number is stored. Only pass a query when the customer tells "
				"you their name or company. Always do this before creating an order."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"query": {
						"type": "string",
						"description": "Their name or company, only if they told you.",
					}
				},
			},
		},
		{
			"name": "link_customer",
			"description": (
				"Remember that this number belongs to a customer, once they have confirmed "
				"it. Future conversations then skip the question."
			),
			"parameters": {
				"type": "object",
				"properties": {"customer": {"type": "string", "description": "The exact customer name."}},
				"required": ["customer"],
			},
		},
	]

	if "Customer" in permitted.get("create", set()):
		schemas.append(
			{
				"name": "create_customer",
				"description": (
					"Create a new customer, only after asking them and them saying yes. Never "
					"create one to get past a failed lookup."
				),
				"parameters": {
					"type": "object",
					"properties": {
						"customer_name": {"type": "string", "description": "Their name or company."},
						"customer_group": {"type": "string"},
						"territory": {"type": "string"},
					},
					"required": ["customer_name"],
				},
			}
		)

	return schemas


def permitted_operations(settings) -> dict[str, set[str]]:
	"""Which doctypes allow which operation, straight from the policy table."""
	result: dict[str, set[str]] = {}

	for row in settings.doctype_policies:
		for operation, (field, _perm) in policy.OPERATIONS.items():
			if row.get(field):
				result.setdefault(operation, set()).add(row.document_type)

	return result


# Arguments declared as JSON strings, because provider schemas cannot express
# a free-form object reliably.
JSON_ARGUMENTS = ("filters", "values")


def decode_json_arguments(arguments: dict) -> dict:
	"""Parse the arguments the model had to send as JSON text."""
	decoded = dict(arguments)

	for key in JSON_ARGUMENTS:
		value = decoded.get(key)
		if value is None or isinstance(value, dict):
			continue

		if not isinstance(value, str):
			raise ValueError(_("{0} must be a JSON object.").format(key))

		text = value.strip()
		if not text:
			decoded.pop(key, None)
			continue

		try:
			parsed = json.loads(text)
		except ValueError:
			raise ValueError(
				_("{0} must be valid JSON. Received: {1}").format(key, text[:200])
			)

		if not isinstance(parsed, dict):
			raise ValueError(_("{0} must be a JSON object, not a {1}.").format(key, type(parsed).__name__))

		decoded[key] = parsed

	return decoded


def call(name: str, arguments: dict, ctx) -> dict:
	"""Run one tool call and always return something the model can read.

	Errors come back as data rather than exceptions: the model needs to be told
	it was refused so it can explain that to the customer, instead of the whole
	turn collapsing.
	"""
	handler = HANDLERS.get(name)
	if not handler:
		return {"error": _("There is no tool called {0}.").format(name)}

	# Some tools act on the conversation rather than a document, so there is no
	# doctype to check a policy against.
	if TOOL_OPERATIONS.get(name) is None and name in HANDLERS:
		try:
			return handler(ctx, **(arguments or {}))
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"AgentX tool failed: {name}")
			return {"error": _("{0} failed.").format(name)}

	try:
		arguments = decode_json_arguments(arguments or {})
	except ValueError as exc:
		return {"error": str(exc)}

	try:
		return handler(ctx, **arguments)

	except policy.PolicyError as exc:
		return {"error": str(exc), "refused": True}

	except TypeError as exc:
		# Wrong or missing arguments; tell the model what it did wrong.
		return {"error": _("Wrong arguments for {0}: {1}").format(name, exc)}

	except frappe.PermissionError as exc:
		return {"error": str(exc) or _("Permission denied."), "refused": True}

	except frappe.ValidationError as exc:
		return {"error": str(exc)}

	except Exception:
		frappe.log_error(frappe.get_traceback(), f"AgentX tool failed: {name}")
		return {"error": _("{0} failed unexpectedly. A person will need to look at this.").format(name)}
