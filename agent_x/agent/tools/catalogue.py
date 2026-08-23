"""Browsing what is for sale.

A customer asking "what do you have?" should not turn into the model guessing
item codes. These tools return real items with real prices and real stock, so
every code that later reaches a Sales Order came from the database.

ERPNext is optional: if Item does not exist, the tools are never advertised.
"""

import frappe
from frappe import _
from frappe.utils import flt, nowdate

MAX_ITEMS = 25

# Fields worth showing a customer. Anything costing-related stays out.
ITEM_FIELDS = ("item_code", "item_name", "item_group", "stock_uom", "description", "brand")


def available() -> bool:
	"""Whether this site sells anything through ERPNext."""
	return bool(frappe.db.exists("DocType", "Item"))


def price_list(settings) -> str | None:
	"""Which price list to quote from."""
	if settings.default_price_list:
		return settings.default_price_list

	try:
		return frappe.get_cached_value("Selling Settings", None, "selling_price_list")
	except Exception:
		return None


def short(text: str | None, limit: int = 160) -> str:
	"""Descriptions are HTML and often long; a chat needs neither."""
	if not text:
		return ""

	cleaned = frappe.utils.strip_html(str(text)).strip()
	cleaned = " ".join(cleaned.split())
	return cleaned if len(cleaned) <= limit else cleaned[:limit].rsplit(" ", 1)[0] + "..."


def prices_for(item_codes: list[str], listname: str | None, customer: str | None = None) -> dict:
	"""Current selling price per item, in one query rather than one each."""
	if not (item_codes and listname):
		return {}

	today = nowdate()
	rows = frappe.get_all(
		"Item Price",
		filters={
			"item_code": ("in", item_codes),
			"price_list": listname,
			"selling": 1,
		},
		fields=["item_code", "price_list_rate", "currency", "valid_from", "valid_upto", "customer"],
		# A customer-specific price should win over the general one, so read the
		# general rows first and let the specific ones overwrite them.
		order_by="customer asc",
	)

	prices: dict[str, dict] = {}
	for row in rows:
		if row.valid_from and str(row.valid_from) > today:
			continue
		if row.valid_upto and str(row.valid_upto) < today:
			continue
		if row.customer and row.customer != customer:
			continue

		prices[row.item_code] = {"rate": flt(row.price_list_rate), "currency": row.currency}

	return prices


def stock_for(item_codes: list[str]) -> dict:
	"""Total sellable quantity per item across warehouses.

	Aggregated through the query builder, because Frappe rejects SQL functions
	written as plain strings in `fields`.
	"""
	if not item_codes:
		return {}

	from frappe.query_builder.functions import Sum

	bin_table = frappe.qb.DocType("Bin")
	rows = (
		frappe.qb.from_(bin_table)
		.select(bin_table.item_code, Sum(bin_table.actual_qty).as_("qty"))
		.where(bin_table.item_code.isin(item_codes))
		.groupby(bin_table.item_code)
	).run(as_dict=True)

	return {row["item_code"]: flt(row["qty"]) for row in rows}


def decorate(ctx, rows: list[dict], settings) -> list[dict]:
	"""Attach price and stock to plain item rows."""
	codes = [r["item_code"] for r in rows]
	customer = ctx.contact.customer if ctx.contact else None

	prices = prices_for(codes, price_list(settings), customer)
	stock = stock_for(codes) if settings.show_stock_levels else {}

	out = []
	for row in rows:
		entry = {
			"item_code": row["item_code"],
			"item_name": row.get("item_name"),
			"uom": row.get("stock_uom"),
		}
		if row.get("item_group"):
			entry["group"] = row["item_group"]
		if row.get("description"):
			entry["description"] = short(row["description"])

		price = prices.get(row["item_code"])
		if price:
			entry["price"] = price["rate"]
			entry["currency"] = price["currency"]
		else:
			# Say so rather than leaving the model to invent a number.
			entry["price"] = None
			entry["note"] = _("No price is set for this item. A person must quote it.")

		if settings.show_stock_levels:
			entry["in_stock"] = stock.get(row["item_code"], 0)

		out.append(entry)

	return out


def base_filters(settings) -> dict:
	filters = {"disabled": 0, "is_sales_item": 1, "has_variants": 0}

	if settings.catalogue_item_group:
		filters["item_group"] = settings.catalogue_item_group

	return filters


def find_items(
	ctx,
	query: str | None = None,
	item_group: str | None = None,
	limit: int = 10,
	in_stock_only: bool = False,
) -> dict:
	"""Search what is for sale."""
	guard(ctx)

	from agent_x.agentx.doctype.agent_action.agent_action import switch_user

	settings = ctx.settings
	limit = max(1, min(int(limit or 10), MAX_ITEMS))

	filters = base_filters(settings)
	if item_group:
		filters["item_group"] = item_group

	or_filters = None
	if query and query.strip():
		term = f"%{query.strip()}%"
		# Customers say the name, not the code, so search both.
		or_filters = {"item_code": ("like", term), "item_name": ("like", term)}

	with switch_user(ctx.acting_user):
		rows = frappe.get_all(
			"Item",
			filters=filters,
			or_filters=or_filters,
			fields=list(ITEM_FIELDS),
			limit=limit + 1,
			order_by="item_name asc",
		)

		more = len(rows) > limit
		rows = rows[:limit]
		items = decorate(ctx, rows, settings)

	if in_stock_only or settings.hide_out_of_stock:
		items = [i for i in items if (i.get("in_stock") or 0) > 0]

	result = {"count": len(items), "items": items}

	if more:
		result["note"] = _("There are more items than shown. Narrow the search to see the rest.")
	if not items:
		result["note"] = _("Nothing matched. Try a different word, or ask what groups exist.")

	return result


def get_item(ctx, item_code: str) -> dict:
	"""One item in detail, for confirming before it goes on an order."""
	guard(ctx)

	from agent_x.agentx.doctype.agent_action.agent_action import switch_user

	with switch_user(ctx.acting_user):
		if not frappe.db.exists("Item", item_code):
			return {"found": False, "item_code": item_code}

		row = frappe.db.get_value("Item", item_code, list(ITEM_FIELDS), as_dict=True)
		if row.get("disabled") is None:
			row["item_code"] = item_code

		items = decorate(ctx, [row], ctx.settings)

	return {"found": True, **items[0]}


def list_item_groups(ctx) -> dict:
	"""What kinds of thing are sold, for a customer who does not know what to ask for."""
	guard(ctx)

	from agent_x.agentx.doctype.agent_action.agent_action import switch_user

	settings = ctx.settings

	from frappe.query_builder.functions import Count

	item = frappe.qb.DocType("Item")
	query = (
		frappe.qb.from_(item)
		.select(item.item_group, Count(item.name).as_("items"))
		.groupby(item.item_group)
		.orderby("items", order=frappe.qb.desc)
		.limit(25)
	)

	for field, value in base_filters(settings).items():
		query = query.where(item[field] == value)

	with switch_user(ctx.acting_user):
		rows = query.run(as_dict=True)

	return {
		"groups": [
			{"group": r["item_group"], "items": r["items"]} for r in rows if r.get("item_group")
		]
	}


def guard(ctx) -> None:
	"""The catalogue is still reading Items, so the Item policy applies."""
	from agent_x.agent import policy

	if not available():
		frappe.throw(_("This site has no item catalogue."))

	if not ctx.settings.enable_catalogue:
		frappe.throw(_("The item catalogue is switched off in AgentX Settings."))

	policy.check(ctx.settings, "Item", "read", ctx.acting_user).raise_if_denied()
