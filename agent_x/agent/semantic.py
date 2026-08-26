"""Matching a message to a canned answer by meaning rather than by wording.

An exact rule catches "what are your opening hours". It misses "what time do
you guys open", "r u open now", and "till what time today", which are the same
question. Writing every phrasing by hand is a losing game.

Embedding the message and comparing it to the rule's phrasings catches all of
them. The point is cost: an embedding is a fraction of a generation call and
usually draws on a separate quota, so a matched rule still answers for nothing
in generation terms.

This is the useful application of vectors here. Retrieval for the prompt saves
only whatever business context would have been pasted into it; avoiding the
generation call saves the whole call.
"""

import hashlib

import frappe

from agent_x.agent.knowledge import embed, pack, unpack

CACHE_PREFIX = "agentx:msgvec:"
MATRIX_KEY = "agentx:rulevecs"

# How long a message embedding is worth keeping. Customers repeat themselves,
# and an identical question should not be embedded twice.
CACHE_SECONDS = 86400


def fingerprint(text: str) -> str:
	return hashlib.sha256(" ".join((text or "").lower().split()).encode()).hexdigest()[:32]


def embed_one(text: str, settings) -> list[float] | None:
	"""The vector for a message, from cache when we have seen it before."""
	key = CACHE_PREFIX + fingerprint(text)

	try:
		cached = frappe.cache.get_value(key)
		if cached:
			return unpack(cached)
	except Exception:
		pass

	try:
		vectors = embed([text], settings)
	except Exception:
		# No key, no quota, no network. Semantic matching is an optimisation;
		# losing it must not lose the reply.
		frappe.log_error(frappe.get_traceback(), "AgentX: could not embed a message")
		return None

	if not vectors:
		return None

	try:
		frappe.cache.set_value(key, pack(vectors[0]), expires_in_sec=CACHE_SECONDS)
	except Exception:
		pass

	return vectors[0]


def build_rule_vectors(rule, settings) -> str | None:
	"""Embed every phrasing on a rule. Called when the rule is saved."""
	phrases = rule.patterns()
	if not phrases:
		return None

	vectors = embed(phrases, settings)
	if len(vectors) != len(phrases):
		frappe.throw(frappe._("The embedding service returned the wrong number of vectors."))

	# One blob per rule, newline separated, so a rule is one row.
	return "\n".join(pack(v) for v in vectors)


def clear_matrix() -> None:
	try:
		frappe.cache.delete_value(MATRIX_KEY)
	except Exception:
		pass


def rule_matrix():
	"""Every semantic rule's vectors as one normalised array, cached."""
	import numpy

	try:
		cached = frappe.cache.get_value(MATRIX_KEY)
	except Exception:
		cached = None

	if cached and cached.get("names"):
		import base64

		matrix = numpy.frombuffer(base64.b64decode(cached["matrix"]), dtype=numpy.float32)
		return cached["names"], matrix.reshape(len(cached["names"]), cached["dimensions"])

	rows = frappe.get_all(
		"WhatsApp Reply Rule",
		filters={"enabled": 1, "match_type": "Semantic", "vectors": ("is", "set")},
		fields=["name", "vectors"],
	)

	names, vectors = [], []
	for row in rows:
		for blob in (row.vectors or "").splitlines():
			blob = blob.strip()
			if not blob:
				continue
			try:
				vectors.append(unpack(blob))
				names.append(row.name)
			except Exception:
				continue

	if not vectors:
		return [], None

	# A rule embedded with a different model has a different width; keep the
	# majority rather than crashing on the stack.
	width = max({len(v) for v in vectors}, key=[len(v) for v in vectors].count)
	pairs = [(n, v) for n, v in zip(names, vectors) if len(v) == width]
	if not pairs:
		return [], None

	names = [n for n, _ in pairs]
	matrix = numpy.array([v for _, v in pairs], dtype=numpy.float32)

	norms = numpy.linalg.norm(matrix, axis=1, keepdims=True)
	norms[norms == 0] = 1.0
	matrix = matrix / norms

	try:
		import base64

		frappe.cache.set_value(
			MATRIX_KEY,
			{
				"names": names,
				"dimensions": width,
				"matrix": base64.b64encode(matrix.astype(numpy.float32).tobytes()).decode("ascii"),
			},
			expires_in_sec=3600,
		)
	except Exception:
		pass

	return names, matrix


def best_rule(text: str, settings):
	"""The rule whose meaning is closest to this message, if close enough."""
	import numpy

	names, matrix = rule_matrix()
	if not names or matrix is None:
		return None, 0.0

	needle = embed_one(text, settings)
	if not needle:
		return None, 0.0

	vector = numpy.array(needle, dtype=numpy.float32)
	if vector.shape[0] != matrix.shape[1]:
		# The embedding model changed and the rules have not caught up.
		return None, 0.0

	norm = numpy.linalg.norm(vector) or 1.0
	scores = matrix @ (vector / norm)

	best = int(numpy.argmax(scores))
	score = float(scores[best])

	floor = float(settings.semantic_threshold or 0.80)
	if score < floor:
		return None, score

	return names[best], score
