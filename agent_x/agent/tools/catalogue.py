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


# ---------------------------------------------------------------- matching
#
# A customer's list never uses your item codes. It says "andolex rinse 200ml"
# where the system says "ANDOLEX -C ORAL RINSE 200ML". Matching has to happen
# against the database, because a code the model invents will fail on save and,
# worse, might match the wrong product.

import re
from difflib import SequenceMatcher

# Words that carry no distinguishing weight in a product name.
NOISE = {"the", "a", "of", "and", "for", "with", "pack", "pcs", "pc", "box", "bottle"}

# Below this nothing is offered at all. Set well clear of the noise: unrelated
# wording scores around 0.15 to 0.45 against a big catalogue, and offering one
# of those as a maybe invites the wrong product onto an order.
FLOOR = 0.60
# At or above this a single match is treated as certain enough to propose.
CONFIDENT = 0.82


def normalise_name(text: str) -> str:
	text = re.sub(r"[^a-z0-9\s.]", " ", str(text or "").lower())
	return " ".join(text.split())


def tokens(text: str) -> list[str]:
	return [t for t in normalise_name(text).split() if t and t not in NOISE]


def score(query: str, candidate: str) -> float:
	"""How well a customer's wording matches a real item name.

	Combines whole-string similarity with token overlap, because a customer
	writes fewer words than the catalogue does and pure string similarity
	punishes that unfairly.
	"""
	a, b = normalise_name(query), normalise_name(candidate)
	if not a or not b:
		return 0.0

	if a == b:
		return 1.0

	ratio = SequenceMatcher(None, a, b).ratio()

	qt, ct = set(tokens(query)), set(tokens(candidate))
	overlap = len(qt & ct) / len(qt) if qt else 0.0

	# A size or strength that appears in both is a strong signal.
	numbers = {t for t in qt if any(c.isdigit() for c in t)}
	if numbers and numbers <= ct:
		overlap = min(1.0, overlap + 0.15)

	return max(ratio, (ratio + overlap * 2) / 3)


def candidates_for(ctx, query: str, settings, limit: int = 60) -> list[dict]:
	"""A shortlist from the database to score locally."""
	from agent_x.agentx.doctype.agent_action.agent_action import switch_user

	filters = base_filters(settings)
	words = [t for t in tokens(query) if len(t) > 2][:4]

	rows, seen = [], set()

	with switch_user(ctx.acting_user):
		for word in words or [query]:
			term = f"%{word}%"
			found = frappe.get_all(
				"Item",
				filters=filters,
				or_filters={"item_code": ("like", term), "item_name": ("like", term)},
				fields=list(ITEM_FIELDS),
				limit=limit,
			)
			for row in found:
				if row["item_code"] not in seen:
					seen.add(row["item_code"])
					rows.append(row)

	# No fallback to an arbitrary slice of the catalogue. Scoring random items
	# against wording that matched nothing produces a plausible-looking match
	# for a product the customer never mentioned.
	return rows


def match_items(ctx, requests: list | str, limit_per_line: int = 3) -> dict:
	"""Resolve a customer's wording to real items.

	Each line comes back with its best matches and a confidence, so the model
	can place the certain ones and ask about the rest instead of guessing.
	"""
	guard(ctx)

	if isinstance(requests, str):
		requests = frappe.parse_json(requests)

	if not isinstance(requests, list) or not requests:
		frappe.throw(_("Give a list of the items to look up."))

	settings = ctx.settings
	results = []

	for entry in requests[:40]:
		if isinstance(entry, str):
			entry = {"name": entry}
		if not isinstance(entry, dict):
			continue

		wanted = str(entry.get("name") or entry.get("item") or entry.get("description") or "").strip()
		if not wanted:
			continue

		qty = entry.get("qty") or entry.get("quantity")

		scored = sorted(
			(
				{**row, "_score": score(wanted, f"{row.get('item_name') or ''} {row.get('item_code') or ''}")}
				for row in candidates_for(ctx, wanted, settings)
			),
			key=lambda r: r["_score"],
			reverse=True,
		)
		shortlist = [r for r in scored if r["_score"] >= FLOOR][:limit_per_line]

		# Only what is needed to choose between options. A description and a
		# group on every candidate tripled the size of this result, and the
		# whole result is carried into every later call in the same message.
		options = []
		for option, row in zip(decorate(ctx, shortlist, settings), shortlist):
			trimmed = {
				"item_code": option["item_code"],
				"item_name": option.get("item_name"),
				"confidence": round(row["_score"], 2),
			}
			if option.get("price") is not None:
				trimmed["price"] = option["price"]
			if option.get("in_stock") is not None:
				trimmed["in_stock"] = option["in_stock"]
			if option.get("note"):
				trimmed["note"] = option["note"]
			options.append(trimmed)

		best = options[0] if options else None
		results.append(
			{
				"asked_for": wanted,
				"qty": qty,
				"status": "matched"
				if best and best["confidence"] >= CONFIDENT
				else ("uncertain" if options else "not_found"),
				"options": options,
			}
		)

	matched = [r for r in results if r["status"] == "matched"]
	unsure = [r for r in results if r["status"] == "uncertain"]
	missing = [r for r in results if r["status"] == "not_found"]

	return {
		"lines": results,
		"matched": len(matched),
		"uncertain": len(unsure),
		"not_found": len(missing),
		"note": _(
			"Use the item_code from a matched line as it is. For an uncertain line, ask the "
			"customer which option they meant. For a line that was not found, say so plainly "
			"rather than substituting something else."
		),
	}
