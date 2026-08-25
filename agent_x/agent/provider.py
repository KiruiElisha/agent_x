"""Talks to the language model.

The runtime works in one neutral conversation format and this module translates
it per provider, so adding a provider never touches the agent loop.

Neutral turn shapes:
  {"role": "user",      "text": str, "image"/"audio": {"mime_type", "data"} | None}
  {"role": "assistant", "text": str, "tool_calls": [{"id", "name", "args"}]}
  {"role": "tool",      "id": str, "name": str, "result": dict}
"""

import json

import requests

import frappe
from frappe import _

GEMINI_DEFAULT = "https://generativelanguage.googleapis.com"
OPENAI_DEFAULT = "https://api.openai.com"
ANTHROPIC_DEFAULT = "https://api.anthropic.com"


class AIError(frappe.ValidationError):
	pass


class Reply:
	"""What the model said: text, tool calls, and what it cost."""

	def __init__(self, text: str = "", tool_calls: list | None = None, usage: dict | None = None):
		self.text = text or ""
		self.tool_calls = tool_calls or []
		self.usage = usage or {}

	@property
	def wants_tools(self) -> bool:
		return bool(self.tool_calls)


def complete(settings, system: str, turns: list[dict], tools: list[dict] | None = None) -> Reply:
	"""One round trip to the model."""
	api_key = settings.get_password("ai_api_key", raise_exception=False)
	if not api_key:
		frappe.throw(_("Set the AI API Key in AgentX Settings."))

	provider = settings.ai_provider or "Google Gemini"

	try:
		if provider == "Google Gemini":
			return call_gemini(settings, system, turns, tools, api_key)
		if provider == "OpenAI":
			return call_openai(settings, system, turns, tools, api_key)
		if provider == "Anthropic Claude":
			return call_anthropic(settings, system, turns, tools, api_key)
	except requests.RequestException as exc:
		raise AIError(_("Could not reach the {0} API: {1}").format(provider, exc)) from exc

	frappe.throw(_("Unsupported AI provider: {0}").format(provider))


def group_turns(turns: list[dict]) -> list[dict]:
	"""Bundle consecutive tool results into one turn.

	When the model calls several tools at once the runtime appends one tool turn
	each. Gemini and Anthropic both want every result for a given assistant turn
	delivered in the single turn that follows it, so merge them here rather than
	making the runtime care which provider it is talking to.
	"""
	grouped: list[dict] = []

	for turn in turns:
		if turn["role"] != "tool":
			grouped.append(turn)
			continue

		if grouped and grouped[-1]["role"] == "tool_group":
			grouped[-1]["items"].append(turn)
		else:
			grouped.append({"role": "tool_group", "items": [turn]})

	return grouped


# ---------------------------------------------------------------------- Gemini


def call_gemini(settings, system, turns, tools, api_key) -> Reply:
	base = (settings.ai_api_base_url or GEMINI_DEFAULT).rstrip("/")
	model = settings.ai_model or "gemini-2.5-flash"

	payload = {
		"systemInstruction": {"parts": [{"text": system}]},
		"contents": [gemini_turn(turn) for turn in group_turns(turns)],
		"generationConfig": {
			"temperature": settings.ai_temperature or 0.3,
			"maxOutputTokens": settings.ai_max_output_tokens or 2048,
		},
	}

	if tools:
		payload["tools"] = [{"functionDeclarations": [gemini_declaration(t) for t in tools]}]
		payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

	response = requests.post(
		f"{base}/v1beta/models/{model}:generateContent",
		json=payload,
		headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
		timeout=settings.request_timeout or 30,
	)
	body = check(response, "Google Gemini")

	candidates = body.get("candidates") or []
	if not candidates:
		# A prompt blocked by safety filters comes back with no candidates.
		blocked = (body.get("promptFeedback") or {}).get("blockReason")
		if blocked:
			raise AIError(_("Gemini blocked the request: {0}").format(blocked))
		raise AIError(_("Gemini returned no reply."))

	parts = (candidates[0].get("content") or {}).get("parts") or []

	text = "".join(p.get("text", "") for p in parts if "text" in p)
	tool_calls = [
		{
			# Gemini has no call id, so pair responses by name instead.
			"id": part["functionCall"].get("name"),
			"name": part["functionCall"].get("name"),
			"args": part["functionCall"].get("args") or {},
		}
		for part in parts
		if "functionCall" in part
	]

	usage = body.get("usageMetadata") or {}
	return Reply(
		text,
		tool_calls,
		{
			"input_tokens": usage.get("promptTokenCount"),
			"output_tokens": usage.get("candidatesTokenCount"),
		},
	)


def gemini_turn(turn: dict) -> dict:
	role = turn["role"]

	if role == "tool_group":
		# Function results go back in one user turn; Gemini only accepts the two roles.
		return {
			"role": "user",
			"parts": [
				{
					"functionResponse": {
						"name": item["name"],
						"response": wrap_result(item["result"]),
					}
				}
				for item in turn["items"]
			],
		}

	if role == "assistant":
		parts = []
		if turn.get("text"):
			parts.append({"text": turn["text"]})
		for call in turn.get("tool_calls") or []:
			parts.append({"functionCall": {"name": call["name"], "args": call.get("args") or {}}})
		return {"role": "model", "parts": parts or [{"text": ""}]}

	parts = [{"text": turn.get("text") or ""}]

	# Gemini takes images and audio through the same inline part.
	for kind in ("image", "audio", "document"):
		blob = turn.get(kind)
		if blob:
			parts.append({"inlineData": {"mimeType": blob["mime_type"], "data": blob["data"]}})

	return {"role": "user", "parts": parts}


def gemini_declaration(tool: dict) -> dict:
	"""Gemini rejects unknown schema keys, so send only what it understands."""
	return {
		"name": tool["name"],
		"description": tool["description"],
		"parameters": clean_schema(tool["parameters"]),
	}


ALLOWED_SCHEMA_KEYS = {"type", "description", "enum", "properties", "required", "items", "nullable"}


def clean_schema(schema: dict) -> dict:
	cleaned = {k: v for k, v in schema.items() if k in ALLOWED_SCHEMA_KEYS}

	if "properties" in cleaned:
		cleaned["properties"] = {k: clean_schema(v) for k, v in cleaned["properties"].items()}
	if "items" in cleaned and isinstance(cleaned["items"], dict):
		cleaned["items"] = clean_schema(cleaned["items"])

	return cleaned


def wrap_result(result) -> dict:
	"""Gemini insists a functionResponse payload is an object."""
	return result if isinstance(result, dict) else {"result": result}


# ---------------------------------------------------------------------- OpenAI


def call_openai(settings, system, turns, tools, api_key) -> Reply:
	base = (settings.ai_api_base_url or OPENAI_DEFAULT).rstrip("/")

	messages = [{"role": "system", "content": system}]
	for turn in group_turns(turns):
		messages.extend(openai_turn(turn))

	payload = {
		"model": settings.ai_model or "gpt-4o-mini",
		"temperature": settings.ai_temperature or 0.3,
		"max_tokens": settings.ai_max_output_tokens or 2048,
		"messages": messages,
	}

	if tools:
		payload["tools"] = [
			{"type": "function", "function": {**t, "parameters": t["parameters"]}} for t in tools
		]
		payload["tool_choice"] = "auto"

	response = requests.post(
		f"{base}/v1/chat/completions",
		json=payload,
		headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
		timeout=settings.request_timeout or 30,
	)
	body = check(response, "OpenAI")

	choices = body.get("choices") or []
	if not choices:
		raise AIError(_("OpenAI returned no reply."))

	message = choices[0].get("message") or {}
	tool_calls = [
		{
			"id": call.get("id"),
			"name": (call.get("function") or {}).get("name"),
			"args": parse_arguments((call.get("function") or {}).get("arguments")),
		}
		for call in message.get("tool_calls") or []
	]

	usage = body.get("usage") or {}
	return Reply(
		message.get("content") or "",
		tool_calls,
		{"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")},
	)


def openai_turn(turn: dict) -> list[dict]:
	role = turn["role"]

	if role == "tool_group":
		# OpenAI wants one tool message per call, not a bundle.
		return [
			{
				"role": "tool",
				"tool_call_id": item["id"],
				"content": json.dumps(item["result"], default=str),
			}
			for item in turn["items"]
		]

	if role == "assistant":
		message = {"role": "assistant", "content": turn.get("text") or None}
		if turn.get("tool_calls"):
			message["tool_calls"] = [
				{
					"id": call["id"],
					"type": "function",
					"function": {"name": call["name"], "arguments": json.dumps(call.get("args") or {})},
				}
				for call in turn["tool_calls"]
			]
		return [message]

	image = turn.get("image")
	if image:
		return [
			{
				"role": "user",
				"content": [
					{"type": "text", "text": turn.get("text") or ""},
					{
						"type": "image_url",
						"image_url": {"url": f"data:{image['mime_type']};base64,{image['data']}"},
					},
				],
			}
		]

	return [{"role": "user", "content": turn.get("text") or ""}]


def parse_arguments(raw) -> dict:
	if isinstance(raw, dict):
		return raw
	try:
		parsed = json.loads(raw or "{}")
		return parsed if isinstance(parsed, dict) else {}
	except ValueError:
		return {}


# ------------------------------------------------------------------- Anthropic


def call_anthropic(settings, system, turns, tools, api_key) -> Reply:
	base = (settings.ai_api_base_url or ANTHROPIC_DEFAULT).rstrip("/")

	payload = {
		"model": settings.ai_model or "claude-sonnet-5",
		"system": system,
		"temperature": settings.ai_temperature or 0.3,
		"max_tokens": settings.ai_max_output_tokens or 2048,
		"messages": [anthropic_turn(turn) for turn in group_turns(turns)],
	}

	if tools:
		payload["tools"] = [
			{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
			for t in tools
		]

	response = requests.post(
		f"{base}/v1/messages",
		json=payload,
		headers={
			"x-api-key": api_key,
			"anthropic-version": "2023-06-01",
			"Content-Type": "application/json",
		},
		timeout=settings.request_timeout or 30,
	)
	body = check(response, "Anthropic Claude")

	blocks = body.get("content") or []
	text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
	tool_calls = [
		{"id": b.get("id"), "name": b.get("name"), "args": b.get("input") or {}}
		for b in blocks
		if b.get("type") == "tool_use"
	]

	usage = body.get("usage") or {}
	return Reply(
		text,
		tool_calls,
		{"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")},
	)


def anthropic_turn(turn: dict) -> dict:
	role = turn["role"]

	if role == "tool_group":
		# Anthropic requires every tool_result for one assistant turn in a single
		# user message, in the order the tools were called.
		return {
			"role": "user",
			"content": [
				{
					"type": "tool_result",
					"tool_use_id": item["id"],
					"content": json.dumps(item["result"], default=str),
				}
				for item in turn["items"]
			],
		}

	if role == "assistant":
		content = []
		if turn.get("text"):
			content.append({"type": "text", "text": turn["text"]})
		for call in turn.get("tool_calls") or []:
			content.append(
				{"type": "tool_use", "id": call["id"], "name": call["name"], "input": call.get("args") or {}}
			)
		return {"role": "assistant", "content": content or [{"type": "text", "text": ""}]}

	image = turn.get("image")
	if image:
		return {
			"role": "user",
			"content": [
				{
					"type": "image",
					"source": {
						"type": "base64",
						"media_type": image["mime_type"],
						"data": image["data"],
					},
				},
				{"type": "text", "text": turn.get("text") or ""},
			],
		}

	return {"role": "user", "content": turn.get("text") or ""}


# ---------------------------------------------------------------------- shared


def check(response: requests.Response, provider: str) -> dict:
	try:
		body = response.json()
	except ValueError:
		raise AIError(
			_("{0} returned a non-JSON response ({1}): {2}").format(
				provider, response.status_code, (response.text or "")[:400]
			)
		)

	if response.status_code >= 400:
		detail = body.get("error") or body
		if isinstance(detail, dict):
			detail = detail.get("message") or json.dumps(detail)
		raise AIError(_("{0} error {1}: {2}").format(provider, response.status_code, str(detail)[:400]))

	return body


def ping(settings, message: str) -> dict:
	"""One plain exchange, used by the Test AI button."""
	reply = complete(
		settings,
		"You are a helpful assistant. Answer in one short sentence.",
		[{"role": "user", "text": message}],
	)

	return {
		"provider": settings.ai_provider,
		"model": settings.ai_model,
		"reply": reply.text,
		"usage": reply.usage,
	}
