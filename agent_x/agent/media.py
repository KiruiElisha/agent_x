"""Fetching an attachment and handing it to the model.

A customer sending a photo of a stock list, or a PDF order form, is one of the
most useful things they can do, and it only works if the file itself reaches the
model. Text alone ("the person sent a document") is not enough to order from.

Gemini reads images and PDFs natively, so both go inline. Anything else is
described but not sent, because a spreadsheet or a Word file would arrive as
bytes the model cannot interpret.
"""

import base64

import requests

import frappe

IMAGE_MIMES = {
	"jpg": "image/jpeg",
	"jpeg": "image/jpeg",
	"png": "image/png",
	"webp": "image/webp",
	"gif": "image/gif",
	"heic": "image/heic",
	"heif": "image/heif",
	"bmp": "image/bmp",
}

# What Gemini can actually read as a document.
READABLE_MIMES = {
	"pdf": "application/pdf",
	"txt": "text/plain",
	"csv": "text/csv",
	"md": "text/markdown",
	"html": "text/html",
	"rtf": "text/rtf",
}

# Sent, but the model will only see bytes. Named so the reply can say why.
UNREADABLE = {
	"xlsx": "a spreadsheet",
	"xls": "a spreadsheet",
	"docx": "a Word document",
	"doc": "a Word document",
	"pptx": "a slideshow",
	"zip": "a zip file",
}


def extension(media: dict) -> str:
	name = (media.get("filename") or media.get("url") or "").lower().split("?")[0]
	return name.rsplit(".", 1)[-1] if "." in name else ""


def declared_mime(media: dict) -> str:
	mime = (media.get("mimetype") or "").split(";")[0].strip().lower()
	return mime


def classify(media: dict) -> str:
	"""image, document, unreadable, or none."""
	if not media:
		return "none"

	mime = declared_mime(media)
	ext = extension(media)

	if mime.startswith("image/") or ext in IMAGE_MIMES:
		return "image"

	if mime == "application/pdf" or ext in READABLE_MIMES:
		return "document"

	if mime.startswith("text/"):
		return "document"

	if ext in UNREADABLE:
		return "unreadable"

	return "none"


def mime_for(media: dict, kind: str) -> str:
	mime = declared_mime(media)
	if mime and "/" in mime:
		return mime

	ext = extension(media)
	if kind == "image":
		return IMAGE_MIMES.get(ext, "image/jpeg")
	return READABLE_MIMES.get(ext, "application/pdf")


def size_cap(settings, kind: str) -> int:
	megabytes = (
		settings.ai_max_image_mb if kind == "image" else settings.max_document_mb
	) or (4 if kind == "image" else 10)
	return int(megabytes) * 1024 * 1024


def fetch(media: dict, cap: int, settings) -> str | None:
	"""The file as base64, however this provider supplies it."""
	inline = media.get("base64")
	if inline:
		# The bridge sends small files inline already.
		return inline if len(inline) * 3 // 4 <= cap else None

	url = media.get("url")
	if not url:
		return None

	try:
		response = requests.get(url, timeout=settings.request_timeout or 30, stream=True)
		if response.status_code >= 400:
			return None

		declared = response.headers.get("Content-Length")
		if declared and int(declared) > cap:
			return None

		# Read with a ceiling, since Content-Length is often missing or wrong.
		chunks, total = [], 0
		for chunk in response.iter_content(65536):
			total += len(chunk)
			if total > cap:
				return None
			chunks.append(chunk)
	except (requests.RequestException, ValueError):
		return None

	if not chunks:
		return None

	return base64.b64encode(b"".join(chunks)).decode("ascii")


def prepare(media: dict, settings) -> dict | None:
	"""What to attach to the model turn, or None if nothing usable.

	Never raises: an attachment that cannot be read should still get a reply,
	just one that says so.
	"""
	kind = classify(media)

	if kind == "unreadable":
		return {"kind": "unreadable", "label": UNREADABLE.get(extension(media), "a file")}

	if kind == "none":
		return None

	if kind == "image" and not settings.ai_read_images:
		return None
	if kind == "document" and not settings.read_documents:
		return None

	try:
		data = fetch(media, size_cap(settings, kind), settings)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "AgentX: could not fetch an attachment")
		return None

	if not data:
		return {"kind": "too_large" if media.get("size") else "unavailable"}

	return {"kind": kind, "mime_type": mime_for(media, kind), "data": data}
