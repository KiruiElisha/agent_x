"""A minimal Frappe stand-in.

The pure logic in this app — phone handling, provider payload shaping, the
policy gate, the confirmation parser — has no business needing a database. This
stub lets those parts be tested with plain `python3`, so a mistake in them shows
up without a site, a bench, or a migration.
"""

import json
import sys
import types


def install() -> None:
	"""Register the fake `frappe` modules in sys.modules."""
	if "frappe" in sys.modules and getattr(sys.modules["frappe"], "_agentx_stub", False):
		return

	frappe = types.ModuleType("frappe")
	frappe._agentx_stub = True

	class ValidationError(Exception):
		pass

	class PermissionError_(Exception):
		pass

	class DoesNotExistError(Exception):
		pass

	frappe.ValidationError = ValidationError
	frappe.PermissionError = PermissionError_
	frappe.DoesNotExistError = DoesNotExistError

	frappe._ = lambda s: s

	def throw(msg, exc=ValidationError):
		raise exc(msg)

	frappe.throw = throw
	frappe.as_json = lambda o, **k: json.dumps(o, default=str)
	frappe.parse_json = lambda s: json.loads(s) if isinstance(s, str) else s
	frappe.get_traceback = lambda: "traceback"
	frappe.log_error = lambda *a, **k: None
	frappe.bold = lambda s: str(s)
	frappe.generate_hash = lambda length=56, *a, **k: "a1b2c3d4e5f6"[:length]
	frappe.msgprint = lambda *a, **k: None
	frappe.only_for = lambda *a, **k: None
	frappe.get_cached_doc = lambda *a, **k: None
	frappe.get_all = lambda *a, **k: []
	frappe.get_doc = lambda *a, **k: None
	frappe.get_meta = lambda *a, **k: None
	frappe.delete_doc = lambda *a, **k: None
	frappe.enqueue = lambda *a, **k: None
	frappe.cache = types.SimpleNamespace(
		get_value=lambda *a, **k: None,
		set_value=lambda *a, **k: None,
		delete_value=lambda *a, **k: None,
	)
	frappe.set_user = lambda u: None
	frappe.session = types.SimpleNamespace(user="Administrator")
	frappe.db = types.SimpleNamespace(
		exists=lambda *a, **k: False,
		get_value=lambda *a, **k: None,
		count=lambda *a, **k: 0,
		set_value=lambda *a, **k: None,
		commit=lambda: None,
		rollback=lambda: None,
	)

	def whitelist(*a, **k):
		def decorate(fn):
			fn.whitelisted = True
			return fn

		return decorate

	frappe.whitelist = whitelist

	model = types.ModuleType("frappe.model")
	document = types.ModuleType("frappe.model.document")

	class Document:
		pass

	document.Document = Document

	utils = types.ModuleType("frappe.utils")
	utils.now_datetime = lambda: None
	utils.add_to_date = lambda *a, **k: None
	utils.add_days = lambda *a, **k: None
	utils.get_url = lambda p="": "http://test" + p
	utils.get_datetime = lambda v: v
	utils.get_time = lambda v: v
	utils.nowdate = lambda: "2026-08-23"

	import datetime as _dt

	def _getdate(v=None):
		if v is None:
			return _dt.date(2026, 8, 23)
		return v if isinstance(v, _dt.date) else _dt.date.fromisoformat(str(v)[:10])

	utils.getdate = _getdate
	utils.add_days = lambda d, days: _getdate(d) + _dt.timedelta(days=days or 0)

	def _flt(v, precision=None):
		try:
			return float(v or 0)
		except (TypeError, ValueError):
			return 0.0

	utils.flt = _flt
	utils.fmt_money = lambda v, currency=None, **k: f"{_flt(v):,.2f}"
	utils.strip_html = lambda t: __import__("re").sub(r"<[^>]+>", "", str(t or ""))
	frappe.utils = utils

	sys.modules["frappe"] = frappe
	sys.modules["frappe.model"] = model
	sys.modules["frappe.model.document"] = document
	sys.modules["frappe.utils"] = utils


class Row(dict):
	"""Stands in for a child table row, which supports both dict and attribute access."""

	def __getattr__(self, key):
		return self.get(key)
