"""Renders a document to PDF and makes it sendable over WhatsApp.

Uses whatever print format the site already has configured for the doctype, so
what the customer receives is what staff see when they hit Print.

There is a privacy wrinkle worth understanding. A hosted provider fetches media
from a URL, so the PDF has to be publicly readable for as long as it takes to
fetch. The self-hosted bridge takes the bytes directly, so its PDFs stay
private. Public files get an unguessable name and are deleted on a schedule.
"""

import base64

import frappe
from frappe import _
from frappe.utils import add_to_date, get_url, now_datetime

# Public PDFs are named so a cleanup job can find them later.
PREFIX = "agentx-"


def resolve_print_format(doctype: str, override: str | None = None) -> str:
	"""The print format this site uses for the doctype.

	Frappe records the default in a Property Setter when someone picks one in
	the UI, and falls back to the doctype's own setting, then Standard.
	"""
	if override:
		if not frappe.db.exists("Print Format", override):
			frappe.throw(_("There is no print format called {0}.").format(override))
		return override

	chosen = frappe.db.get_value(
		"Property Setter",
		{"doc_type": doctype, "property": "default_print_format"},
		"value",
	)
	if chosen and frappe.db.exists("Print Format", chosen):
		return chosen

	meta = frappe.get_meta(doctype)
	if meta.default_print_format and frappe.db.exists("Print Format", meta.default_print_format):
		return meta.default_print_format

	return "Standard"


def render_pdf(
	doctype: str,
	name: str,
	print_format: str | None = None,
	letterhead: str | None = None,
	language: str | None = None,
) -> bytes:
	"""Render one document to PDF bytes."""
	chosen = resolve_print_format(doctype, print_format)

	previous_language = frappe.local.lang
	try:
		if language:
			frappe.local.lang = language

		content = frappe.get_print(
			doctype,
			name,
			print_format=chosen,
			as_pdf=True,
			letterhead=letterhead,
		)
	except Exception as exc:
		# wkhtmltopdf missing or a broken template are the usual causes, and the
		# raw traceback is useless to whoever is reading a WhatsApp log.
		frappe.log_error(frappe.get_traceback(), f"AgentX: could not render {doctype} {name}")
		frappe.throw(
			_("Could not produce a PDF of {0} {1} using print format {2}: {3}").format(
				doctype, name, chosen, str(exc)[:200]
			)
		)
	finally:
		frappe.local.lang = previous_language

	if not content:
		frappe.throw(_("The PDF of {0} {1} came out empty.").format(doctype, name))

	return content


def build_filename(doctype: str, name: str, unique: bool = True) -> str:
	"""A filename a person would recognise in their chat."""
	safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in f"{doctype}-{name}")
	safe = "-".join(part for part in safe.split("-") if part)

	if not unique:
		return f"{safe}.pdf"

	# The random part is what stops a public URL being guessable.
	return f"{PREFIX}{safe}-{frappe.generate_hash(length=12)}.pdf"


def attach_pdf(
	doctype: str,
	name: str,
	content: bytes,
	*,
	private: bool = True,
	filename: str | None = None,
):
	"""Store the PDF as a File against the document."""
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": filename or build_filename(doctype, name, unique=not private),
			"attached_to_doctype": doctype,
			"attached_to_name": name,
			"is_private": 1 if private else 0,
			"content": content,
		}
	)
	file_doc.insert(ignore_permissions=True)
	return file_doc


def prepare(
	doctype: str,
	name: str,
	*,
	needs_public_url: bool,
	print_format: str | None = None,
	letterhead: str | None = None,
	language: str | None = None,
) -> dict:
	"""Render, store, and return everything needed to send the PDF."""
	content = render_pdf(doctype, name, print_format, letterhead, language)

	file_doc = attach_pdf(doctype, name, content, private=not needs_public_url)

	result = {
		"file": file_doc.name,
		"file_url": file_doc.file_url,
		"filename": file_doc.file_name,
		"print_format": resolve_print_format(doctype, print_format),
		"size": len(content),
		"public": bool(needs_public_url),
	}

	if needs_public_url:
		result["url"] = get_url(file_doc.file_url)
	else:
		# The bridge uploads the bytes itself, so nothing is exposed.
		result["base64"] = base64.b64encode(content).decode("ascii")

	return result


def cleanup_public_pdfs() -> None:
	"""Delete the public PDFs the assistant generated. Scheduled hourly.

	They only need to exist long enough for the provider to fetch them, so
	leaving them readable any longer is a needless disclosure.
	"""
	settings = frappe.get_cached_doc("AgentX Settings")
	hours = settings.pdf_retention_hours or 0
	if hours <= 0:
		return

	cutoff = add_to_date(now_datetime(), hours=-hours)

	stale = frappe.get_all(
		"File",
		filters={
			"is_private": 0,
			"file_name": ("like", f"{PREFIX}%"),
			"creation": ("<", cutoff),
		},
		pluck="name",
		limit=500,
	)

	for name in stale:
		try:
			frappe.delete_doc("File", name, ignore_permissions=True, delete_permanently=True)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "AgentX: could not delete generated PDF")

	if stale:
		frappe.db.commit()
