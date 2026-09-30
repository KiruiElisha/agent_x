"""Site clock.

WhatsApp speaks in Unix seconds, which are UTC. Frappe stores naive datetimes
in the time zone set under System Settings, and that is what business hours,
message times, and QR expiry are compared against.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import frappe


def site_timezone() -> str:
	"""System Settings → Time Zone, or UTC when it cannot be read."""
	try:
		zone = frappe.get_system_settings("time_zone")
	except Exception:
		zone = None
	return zone or "UTC"


def to_site_datetime(value, time_zone: str) -> str | None:
	"""Render a UTC instant as a naive site-local datetime string.

	Accepts Unix seconds, an ISO timestamp (including a trailing Z), or a
	datetime. A naive datetime is treated as UTC, because the bridge always
	sends UTC.
	"""
	if value is None or value == "":
		return None

	try:
		moment = _as_utc(value)
		zone = ZoneInfo(time_zone or "UTC")
	except (ValueError, TypeError, OSError, OverflowError):
		return None

	local = moment.astimezone(zone).replace(tzinfo=None)
	return local.strftime("%Y-%m-%d %H:%M:%S")


def _as_utc(value) -> datetime:
	if isinstance(value, datetime):
		moment = value
	elif isinstance(value, (int, float)) or (isinstance(value, str) and str(value).strip().isdigit()):
		moment = datetime.fromtimestamp(int(value), timezone.utc)
	else:
		text = str(value).strip().replace("Z", "+00:00")
		moment = datetime.fromisoformat(text)

	if moment.tzinfo is None:
		moment = moment.replace(tzinfo=timezone.utc)
	return moment
