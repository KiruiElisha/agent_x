"""Phone number handling.

WhatsApp identifies people by a country-coded number with no punctuation, but
users type numbers however they like. Everything entering the system goes
through here so one person is not stored three ways.
"""

import re

import frappe

NON_DIGITS = re.compile(r"\D")

# Enough digits to identify a subscriber almost anywhere, used when comparing
# two numbers that may or may not carry a country code.
COMPARE_TAIL = 9


def digits_only(value: str | None) -> str:
	"""Strip everything that is not a digit."""
	return NON_DIGITS.sub("", str(value or ""))


def normalise(value: str | None, country_code: str | None = None) -> str:
	"""Return a WhatsApp-style number: country code followed by the subscriber number.

	A leading 0 is a national trunk prefix, not part of the number, so it is
	replaced by the country code rather than kept.
	"""
	digits = digits_only(value)
	if not digits:
		return ""

	code = digits_only(country_code)

	# "+254712345678" and "00254712345678" are already international.
	if digits.startswith("00"):
		return digits[2:]

	if not code:
		return digits

	if digits.startswith("0"):
		return code + digits.lstrip("0")

	# Already carries the country code.
	if digits.startswith(code):
		return digits

	# A bare local number, e.g. 712345678.
	return code + digits


def same_number(left: str | None, right: str | None) -> bool:
	"""True when two numbers identify the same person.

	Compares the trailing digits so 0712345678 and 254712345678 match, but only
	when both are long enough that the tail is actually distinguishing.
	"""
	a, b = digits_only(left), digits_only(right)
	if not a or not b:
		return False
	if a == b:
		return True

	if len(a) < COMPARE_TAIL or len(b) < COMPARE_TAIL:
		return False

	return a[-COMPARE_TAIL:] == b[-COMPARE_TAIL:]


def to_jid(number: str | None, is_group: bool = False) -> str:
	"""Build the WhatsApp address for a number."""
	if number and "@" in str(number):
		return str(number)

	digits = digits_only(number)
	if not digits:
		frappe.throw(frappe._("{0} is not a usable phone number.").format(number))

	return f"{digits}@{'g.us' if is_group else 's.whatsapp.net'}"


def from_jid(jid: str | None) -> str:
	"""Pull the number back out of a WhatsApp address."""
	if not jid:
		return ""
	user = str(jid).split("@")[0]
	# Multi-device addresses look like 254712345678:12@s.whatsapp.net.
	return user.split(":")[0]
