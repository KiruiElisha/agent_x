"""Retrieval over a knowledge base, so the prompt carries only what is relevant.

Business context that never changes — policies, delivery terms, FAQs, warranty
rules — costs tokens on every single message when it lives in the system
prompt. Here it is chunked and embedded once by a background job, and each
inbound message pulls back only the few passages that actually relate to it.

That is a trade, not a free win: retrieval adds one small embedding call per
message. It pays for itself when the material is larger than roughly a page,
and costs slightly more when it is not, which is why it is off by default.

Vectors live as base64 float32 on the chunk rows and are searched in process
with numpy. MariaDB has no vector type here, and for the few thousand chunks a
business realistically has, an in-memory dot product is quicker than anything
involving the database.
"""

import base64
import hashlib
import struct

import frappe
from frappe import _
from frappe.utils import now_datetime

# Roughly four characters to a token, which is close enough for sizing chunks.
CHARS_PER_TOKEN = 4

CACHE_KEY = "agentx:knowledge:matrix"
VERSION_KEY = "agentx:knowledge:version"

# Nothing useful is retrieved for "hi" or "yes", so do not spend a call on it.
MIN_QUERY_CHARS = 12
TRIVIAL = {
	"hi", "hello", "hey", "yes", "no", "ok", "okay", "thanks", "thank you",
	"good morning", "good afternoon", "good evening", "sawa", "asante", "ndio", "hapana",
}


class KnowledgeError(frappe.ValidationError):
	pass


# ---------------------------------------------------------------- vector store


def pack(vector: list[float]) -> str:
	"""Store a vector compactly.

	A real 768 float embedding is about 4KB packed against 16KB as JSON, so a
	few thousand chunks stay a few megabytes rather than tens.
	"""
	return base64.b64encode(struct.pack(f"{len(vector)}f", *vector)).decode("ascii")


def unpack(blob: str) -> list[float]:
	raw = base64.b64decode(blob)
	return list(struct.unpack(f"{len(raw) // 4}f", raw))


def bump_version() -> None:
	"""Invalidate the cached matrix. Called whenever chunks change."""
	try:
		frappe.cache.delete_value(CACHE_KEY)
		frappe.cache.delete_value(VERSION_KEY)
	except Exception:
		pass


def load_matrix():
	"""Every chunk vector as one numpy array, cached between requests."""
	import numpy

	cached = None
	try:
		cached = frappe.cache.get_value(CACHE_KEY)
	except Exception:
		pass

	if cached and cached.get("ids"):
		matrix = numpy.frombuffer(base64.b64decode(cached["matrix"]), dtype=numpy.float32)
		return cached["ids"], matrix.reshape(len(cached["ids"]), cached["dimensions"])

	rows = frappe.get_all(
		"Agent Knowledge Chunk",
		filters={"embedding": ("is", "set")},
		fields=["name", "embedding", "dimensions"],
		limit=20000,
	)
	rows = [r for r in rows if r.embedding and r.dimensions]
	if not rows:
		return [], None

	# A mixed set of dimensions means the embedding model changed mid-flight;
	# keep the majority rather than crashing on the stack.
	dimensions = max({r.dimensions for r in rows}, key=[r.dimensions for r in rows].count)
	rows = [r for r in rows if r.dimensions == dimensions]

	ids = [r.name for r in rows]
	matrix = numpy.array([unpack(r.embedding) for r in rows], dtype=numpy.float32)

	# Normalise once, so searching is a plain dot product.
	norms = numpy.linalg.norm(matrix, axis=1, keepdims=True)
	norms[norms == 0] = 1.0
	matrix = matrix / norms

	try:
		frappe.cache.set_value(
			CACHE_KEY,
			{
				"ids": ids,
				"dimensions": dimensions,
				"matrix": base64.b64encode(matrix.astype(numpy.float32).tobytes()).decode("ascii"),
			},
			expires_in_sec=3600,
		)
	except Exception:
		pass

	return ids, matrix


# ------------------------------------------------------------------ embeddings


def embedding_provider(settings) -> tuple[str, str, str]:
	"""Which service embeds text, its key, and the model.

	Anthropic has no embeddings API, so a Claude install still embeds with
	Gemini rather than silently doing nothing.
	"""
	provider = settings.embedding_provider or (
		settings.ai_provider if settings.ai_provider in ("Google Gemini", "OpenAI") else "Google Gemini"
	)

	key = settings.get_password("embedding_api_key", raise_exception=False)
	if not key and provider == settings.ai_provider:
		key = settings.get_password("ai_api_key", raise_exception=False)

	if not key:
		frappe.throw(
			_("Set an Embedding API Key for {0} in AgentX Settings.").format(provider)
		)

	model = settings.embedding_model or (
		"text-embedding-004" if provider == "Google Gemini" else "text-embedding-3-small"
	)
	return provider, key, model


def embed(texts: list[str], settings) -> list[list[float]]:
	"""Turn text into vectors. One call for the whole batch."""
	import requests

	if not texts:
		return []

	provider, key, model = embedding_provider(settings)
	timeout = settings.request_timeout or 30

	try:
		if provider == "Google Gemini":
			base = (settings.ai_api_base_url or "https://generativelanguage.googleapis.com").rstrip("/")
			response = requests.post(
				f"{base}/v1beta/models/{model}:batchEmbedContents",
				json={
					"requests": [
						{"model": f"models/{model}", "content": {"parts": [{"text": t}]}}
						for t in texts
					]
				},
				headers={"x-goog-api-key": key, "Content-Type": "application/json"},
				timeout=timeout,
			)
			body = check(response, provider)
			return [e["values"] for e in body.get("embeddings", [])]

		base = (settings.ai_api_base_url or "https://api.openai.com").rstrip("/")
		response = requests.post(
			f"{base}/v1/embeddings",
			json={"model": model, "input": texts},
			headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
			timeout=timeout,
		)
		body = check(response, provider)
		return [row["embedding"] for row in sorted(body.get("data", []), key=lambda r: r["index"])]

	except requests.RequestException as exc:
		raise KnowledgeError(_("Could not reach the {0} embedding API: {1}").format(provider, exc))


def check(response, provider: str) -> dict:
	import json as jsonlib

	try:
		body = response.json()
	except ValueError:
		raise KnowledgeError(
			_("{0} returned a non-JSON response ({1}).").format(provider, response.status_code)
		)

	if response.status_code >= 400:
		detail = body.get("error") or body
		if isinstance(detail, dict):
			detail = detail.get("message") or jsonlib.dumps(detail)
		raise KnowledgeError(_("{0} error {1}: {2}").format(provider, response.status_code, str(detail)[:300]))

	return body


# -------------------------------------------------------------------- chunking


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
	"""Split on paragraphs, keeping chunks near `size` characters.

	Paragraph boundaries beat a fixed window: a policy split mid-sentence
	retrieves badly, because neither half says the whole rule.
	"""
	text = (text or "").strip()
	if not text:
		return []

	paragraphs = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
	if not paragraphs:
		paragraphs = [text]

	chunks, current = [], ""

	for paragraph in paragraphs:
		# A single huge paragraph still has to be broken up.
		while len(paragraph) > size:
			cut = paragraph.rfind(" ", 0, size)
			cut = cut if cut > size // 2 else size
			piece, paragraph = paragraph[:cut].strip(), paragraph[cut:].strip()
			if current:
				chunks.append(current)
				current = ""
			chunks.append(piece)

		if not paragraph:
			continue

		if len(current) + len(paragraph) + 2 <= size:
			current = f"{current}\n\n{paragraph}" if current else paragraph
		else:
			if current:
				chunks.append(current)
			current = paragraph

	if current:
		chunks.append(current)

	if overlap <= 0 or len(chunks) < 2:
		return chunks

	# Carry the tail of each chunk into the next, so a rule spanning a boundary
	# is still retrievable from either side.
	overlapped = [chunks[0]]
	for previous, nxt in zip(chunks, chunks[1:]):
		tail = previous[-overlap:].strip()
		overlapped.append(f"{tail}\n\n{nxt}" if tail else nxt)

	return overlapped


def estimate_tokens(text: str) -> int:
	return max(1, len(text or "") // CHARS_PER_TOKEN)


def content_of(doc) -> str:
	"""The raw text behind one knowledge record."""
	if doc.source_type == "Text":
		return frappe.utils.strip_html(doc.content or "")

	if doc.source_type == "Document":
		if not (doc.reference_doctype and doc.reference_name):
			return ""
		return document_text(doc)

	if doc.source_type == "File":
		return file_text(doc.attachment)

	return ""


def document_text(doc) -> str:
	target = frappe.get_doc(doc.reference_doctype, doc.reference_name)
	wanted = [f.strip() for f in (doc.reference_fields or "").split(",") if f.strip()]

	lines = [f"{doc.reference_doctype} {doc.reference_name}"]
	for field in target.meta.fields:
		if wanted and field.fieldname not in wanted:
			continue
		if field.fieldtype in ("Section Break", "Column Break", "Tab Break", "HTML", "Button", "Password"):
			continue

		value = target.get(field.fieldname)
		if value in (None, "", []) or isinstance(value, list):
			continue

		lines.append(f"{field.label or field.fieldname}: {frappe.utils.strip_html(str(value))}")

	return "\n".join(lines)


def file_text(file_url: str | None) -> str:
	if not file_url:
		return ""

	try:
		file_doc = frappe.get_doc("File", {"file_url": file_url})
		content = file_doc.get_content()
	except Exception:
		raise KnowledgeError(_("Could not read the attached file."))

	if isinstance(content, bytes):
		try:
			content = content.decode("utf-8")
		except UnicodeDecodeError:
			raise KnowledgeError(_("Only plain text and markdown files can be indexed."))

	return content


# --------------------------------------------------------------------- building


def build(name: str) -> dict:
	"""Chunk and embed one knowledge record. Runs as a background job."""
	doc = frappe.get_doc("Agent Knowledge", name)
	settings = frappe.get_cached_doc("AgentX Settings")

	doc.db_set({"status": "Building", "error": None}, update_modified=False)
	frappe.db.commit()

	try:
		text = content_of(doc)
		if not text.strip():
			raise KnowledgeError(_("There is nothing to index."))

		chunks = chunk_text(
			text,
			size=settings.chunk_size or 1200,
			overlap=settings.chunk_overlap or 150,
		)
		vectors = embed(chunks, settings)

		if len(vectors) != len(chunks):
			raise KnowledgeError(
				_("The embedding service returned {0} vectors for {1} chunks.").format(
					len(vectors), len(chunks)
				)
			)

		frappe.db.delete("Agent Knowledge Chunk", {"knowledge": name})

		for index, (piece, vector) in enumerate(zip(chunks, vectors)):
			frappe.get_doc(
				{
					"doctype": "Agent Knowledge Chunk",
					"knowledge": name,
					"chunk_index": index,
					"content": piece,
					"embedding": pack(vector),
					"dimensions": len(vector),
					"tokens": estimate_tokens(piece),
				}
			).insert(ignore_permissions=True)

		doc.db_set(
			{
				"status": "Ready",
				"chunk_count": len(chunks),
				"tokens_estimated": sum(estimate_tokens(c) for c in chunks),
				"content_hash": hashlib.sha256(text.encode()).hexdigest(),
				"last_built_on": now_datetime(),
				"error": None,
			},
			update_modified=False,
		)
		frappe.db.commit()
		bump_version()

		return {"status": "Ready", "chunks": len(chunks)}

	except Exception as exc:
		frappe.db.rollback()
		doc.db_set({"status": "Failed", "error": str(exc)[:500]}, update_modified=False)
		frappe.db.commit()
		frappe.log_error(frappe.get_traceback(), f"AgentX: could not index {name}")
		raise


def enqueue_build(name: str) -> None:
	frappe.enqueue(
		"agent_x.agent.knowledge.build",
		queue="long",
		timeout=1500,
		name=name,
		job_id=f"agentx-knowledge-{name}",
		deduplicate=True,
	)


def rebuild_stale() -> None:
	"""Re-index anything whose source changed. Scheduled daily."""
	settings = frappe.get_cached_doc("AgentX Settings")
	if not settings.knowledge_enabled:
		return

	for row in frappe.get_all(
		"Agent Knowledge", filters={"enabled": 1}, fields=["name", "content_hash", "status"]
	):
		try:
			doc = frappe.get_doc("Agent Knowledge", row.name)
			current = hashlib.sha256(content_of(doc).encode()).hexdigest()
		except Exception:
			continue

		if row.status != "Ready" or current != (row.content_hash or ""):
			enqueue_build(row.name)


# -------------------------------------------------------------------- searching


def is_trivial(query: str) -> bool:
	"""Whether looking anything up is worth an embedding call."""
	cleaned = (query or "").strip().lower().rstrip("?!.")
	return len(cleaned) < MIN_QUERY_CHARS or cleaned in TRIVIAL


def search(query: str, settings, limit: int | None = None) -> list[dict]:
	"""The passages most related to this message."""
	import numpy

	ids, matrix = load_matrix()
	if not ids or matrix is None:
		return []

	vectors = embed([query], settings)
	if not vectors:
		return []

	needle = numpy.array(vectors[0], dtype=numpy.float32)
	if needle.shape[0] != matrix.shape[1]:
		# The model changed and the index has not caught up yet.
		frappe.log_error(
			f"Query vector is {needle.shape[0]} wide but the index is {matrix.shape[1]}. "
			"Rebuild the knowledge base.",
			"AgentX: embedding dimensions do not match",
		)
		return []

	norm = numpy.linalg.norm(needle) or 1.0
	scores = matrix @ (needle / norm)

	top = int(limit or settings.retrieval_top_k or 4)
	best = numpy.argsort(scores)[::-1][:top]

	floor = float(settings.retrieval_min_score or 0.0)
	picked = [(ids[i], float(scores[i])) for i in best if float(scores[i]) >= floor]
	if not picked:
		return []

	rows = frappe.get_all(
		"Agent Knowledge Chunk",
		filters={"name": ("in", [p[0] for p in picked])},
		fields=["name", "content", "knowledge"],
	)
	by_name = {r.name: r for r in rows}

	return [
		{
			"chunk": name,
			"score": round(score, 4),
			"source": by_name[name].knowledge,
			"content": by_name[name].content,
		}
		for name, score in picked
		if name in by_name
	]


def context_for(message: str, settings) -> tuple[str, list]:
	"""What to put in the prompt for this message, and what it came from."""
	if not settings.knowledge_enabled or is_trivial(message):
		return "", []

	try:
		hits = search(message, settings)
	except Exception:
		# Retrieval is an optimisation. Losing it must never lose the reply.
		frappe.log_error(frappe.get_traceback(), "AgentX: knowledge lookup failed")
		return "", []

	if not hits:
		return "", []

	lines = []
	for hit in hits:
		lines.append(f"[{hit['source']}]\n{hit['content']}")

	return "\n\n".join(lines), hits
