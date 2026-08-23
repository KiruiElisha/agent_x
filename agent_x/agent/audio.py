"""Turning voice notes into text.

Customers send voice notes constantly, and a bot that answers "I cannot listen
to that" is a bot people stop using. Gemini reads audio natively, so a voice
note becomes a normal turn in the conversation.

Where the bytes come from depends on the provider: WaClient hands over a URL,
the self-hosted bridge inlines small audio in the webhook itself.
"""

import base64

import requests

import frappe
from frappe import _

# WhatsApp voice notes are Opus in an Ogg container. The others appear when
# someone forwards a file rather than recording one.
AUDIO_MIMES = {
	"ogg": "audio/ogg",
	"oga": "audio/ogg",
	"opus": "audio/ogg",
	"mp3": "audio/mp3",
	"m4a": "audio/mp4",
	"mp4": "audio/mp4",
	"aac": "audio/aac",
	"wav": "audio/wav",
	"amr": "audio/amr",
	"flac": "audio/flac",
}

AUDIO_TYPES = ("audio", "voice", "ptt")

PROMPT = (
	"Transcribe this voice note exactly, in the language it was spoken. "
	"Return only the words spoken, with no commentary, no timestamps, and no speaker labels. "
	"If nothing intelligible was said, return an empty string."
)


def is_voice(message_type: str | None, media: dict | None) -> bool:
	kind = str(message_type or "").strip().casefold()
	if kind in AUDIO_TYPES:
		return True

	name = ((media or {}).get("filename") or "").casefold()
	return any(name.endswith("." + ext) for ext in AUDIO_MIMES)


def guess_mime(media: dict | None) -> str:
	declared = (media or {}).get("mimetype") or ""
	if declared.startswith("audio/"):
		# Providers append codec parameters, which the model does not want.
		return declared.split(";")[0].strip()

	name = ((media or {}).get("filename") or (media or {}).get("url") or "").casefold()
	for ext, mime in AUDIO_MIMES.items():
		if name.endswith("." + ext):
			return mime

	return "audio/ogg"


def fetch(media: dict, settings) -> str | None:
	"""The audio as base64, however this provider supplies it."""
	# The bridge inlines small audio, so there is nothing to download.
	inline = media.get("base64")
	if inline:
		return inline

	url = media.get("url")
	if not url:
		return None

	cap = (settings.max_audio_mb or 8) * 1024 * 1024

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


def transcribe(media: dict, settings) -> str:
	"""Text of a voice note, or an empty string if it cannot be read.

	Never raises: a voice note that will not transcribe should still get a
	reply, just one that says it could not be heard.
	"""
	if not settings.transcribe_voice_notes:
		return ""

	provider = settings.ai_provider or "Google Gemini"
	if provider != "Google Gemini":
		# Only the Gemini path sends audio parts today.
		return ""

	try:
		audio = fetch(media, settings)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "AgentX: could not fetch a voice note")
		return ""

	if not audio:
		return ""

	try:
		from agent_x.agent import provider as ai

		reply = ai.complete(
			settings,
			PROMPT,
			[{"role": "user", "text": PROMPT, "audio": {"mime_type": guess_mime(media), "data": audio}}],
		)
		return (reply.text or "").strip()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "AgentX: could not transcribe a voice note")
		return ""
