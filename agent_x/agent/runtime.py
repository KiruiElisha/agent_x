"""The agent loop.

One inbound message goes in, one reply comes out. In between the model may call
tools as many times as `max_tool_iterations` allows. Everything the model did is
written to an Agent Run, and every document change to an Agent Action.
"""

import re
import time

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

from agent_x.agent import drafts, handoff, knowledge, policy, prompt, provider, registry, summary
from agent_x.agent.tools import memory
from agent_x.agent.tools.documents import ToolContext
from agent_x.agentx.doctype.agent_conversation import agent_conversation
from agent_x.agentx.doctype.whatsapp_contact.whatsapp_contact import acting_user as resolve_user

# What counts as agreeing to a pending change. Deliberately narrow: a vague
# reply must not be read as consent to change a document.
YES = {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "confirm", "confirmed", "go ahead", "do it", "proceed", "ndio", "sawa"}
NO = {"no", "n", "nope", "cancel", "stop", "don't", "dont", "abort", "hapana"}

MAX_TOOL_RESULT_CHARS = 6000


class AgentResult:
	def __init__(self, reply: str = "", run: str | None = None, sent: bool = False, **extra):
		self.reply = reply
		self.run = run
		self.sent = sent
		self.extra = extra

	def as_dict(self) -> dict:
		return {"reply": self.reply, "run": self.run, "sent": self.sent, **self.extra}


def handle(
	inbound: dict,
	contact,
	settings,
	session: str | None = None,
	exclude_message: str | None = None,
) -> AgentResult | None:
	"""Answer one inbound message. Returns None when nothing should be sent."""
	if not (settings.enabled and settings.ai_enabled):
		return None

	text = (inbound.get("message") or "").strip()

	user = resolve_user(contact, settings)
	conversation = agent_conversation.get_or_create(contact.name, session, user)

	# While a person has the conversation the assistant says nothing at all.
	# Two voices answering one customer is worse than a slow reply.
	if handoff.is_held(conversation, settings):
		return None

	# A pending change owns the next reply: it is a yes or no question.
	if conversation.status == "Awaiting Confirmation":
		answered = resolve_confirmation(conversation, text, settings)
		if answered:
			return answered

	# Asking for a human is answered by fetching one, not by the assistant
	# trying harder.
	if handoff.asked_for_a_person(text, settings):
		handoff.start(conversation, settings, reason=_("The customer asked for a person."))
		return AgentResult(
			(settings.handoff_message or "").strip()
			or _("Let me get a person to help you. Someone will reply shortly.")
		)

	return run_agent(inbound, contact, conversation, settings, user, session, exclude_message)


# ------------------------------------------------------------------ confirmations


def contact_of(conversation):
	try:
		return frappe.get_doc("WhatsApp Contact", conversation.contact)
	except Exception:
		return None


def resolve_confirmation(conversation, text: str, settings) -> AgentResult | None:
	"""Read a yes or no against the change waiting on this conversation."""
	action_name = conversation.pending_action
	if not action_name or not frappe.db.exists("Agent Action", action_name):
		clear_pending(conversation)
		return None

	action = frappe.get_doc("Agent Action", action_name)

	if action.status != "Pending":
		# It was approved or rejected in the desk while they were typing.
		clear_pending(conversation)
		return None

	if conversation.pending_expires_on and now_datetime() > conversation.pending_expires_on:
		action.db_set("status", "Expired", update_modified=False)
		clear_pending(conversation)
		return AgentResult(
			_("That request timed out, so nothing was changed. Tell me again if you still need it.")
		)

	answer = normalise_answer(text)

	if answer == "no":
		action.db_set({"status": "Rejected", "error": "Declined over WhatsApp"}, update_modified=False)
		clear_pending(conversation)
		return AgentResult(_("No problem, I have not changed anything."))

	if answer != "yes":
		# Anything ambiguous is not consent. Ask again rather than guessing.
		return AgentResult(
			_("Sorry, I need a clear answer first. Reply YES to go ahead with: {0}. Reply NO to drop it.").format(
				action.summary
			)
		)

	try:
		# The person who said yes is the contact, acting as their mapped user.
		action.db_set(
			{"status": "Approved", "approved_by": action.acting_user}, update_modified=False
		)
		result = action.execute()
	except Exception as exc:
		clear_pending(conversation)
		frappe.log_error(frappe.get_traceback(), "AgentX: confirmed action failed")
		return AgentResult(_("That did not work: {0}").format(str(exc)[:300]))

	clear_pending(conversation)
	conversation.db_set(
		"action_count", (conversation.action_count or 0) + 1, update_modified=False
	)

	if result.get("dry_run"):
		return AgentResult(_("Dry run is on, so nothing was actually changed."))

	name = result.get("name") or action.document_name

	# A long document is easier to check as a PDF than as a chat message.
	sent = drafts.maybe_send(action, settings, contact_of(conversation), conversation.session)

	message = done_message(action, name)
	if sent:
		message += "\n\n" + _("I have sent you a copy to look over.")

	return AgentResult(message)


DONE = {
	"create": "Done. {0} {1} is created.",
	"update": "Done, {0} {1} is updated.",
	"submit": "Done. {0} {1} is submitted.",
	"cancel": "Done. {0} {1} is cancelled.",
	"delete": "Done. {0} {1} has been deleted.",
}


def done_message(action, name: str | None) -> str:
	template = DONE.get(action.action, "Done. {0} {1}.")
	return _(template).format(action.document_type, name or "")


# Words that turn an apparent yes into something else: a condition, a change of
# mind, or an instruction to do it differently. "Yes but only if..." is not
# consent to what was described.
QUALIFIERS = {
	"but", "however", "if", "unless", "except", "though", "although",
	"instead", "rather", "change", "changed", "different", "differently",
	"only", "actually", "wait", "hold", "first", "before", "dont", "don't", "not",
}


def normalise_answer(text: str) -> str:
	"""Read a yes or a no, and treat anything else as neither.

	A change is about to be written, so the bar is deliberately high: the reply
	has to open with an affirmative and add no conditions to it. "Yes just place
	the order" passes. "Yes but only if it is under 5000" does not, because the
	person is describing a different order from the one they were shown.
	"""
	cleaned = re.sub(r"[^\w\s']", " ", (text or "").strip().lower())
	cleaned = " ".join(cleaned.split())
	if not cleaned:
		return "unclear"

	if cleaned in YES:
		return "yes"
	if cleaned in NO:
		return "no"

	words = cleaned.split()

	# A multi word phrase such as "go ahead" or "do it".
	for size in (3, 2):
		phrase = " ".join(words[:size])
		if phrase in YES:
			return "yes" if not qualified(words[size:]) else "unclear"
		if phrase in NO:
			return "no"

	first = words[0]

	if first in NO:
		return "no"

	if first in YES:
		# Extra words are fine as long as they do not change the request.
		return "unclear" if qualified(words[1:]) else "yes"

	return "unclear"


def qualified(rest: list[str]) -> bool:
	"""Whether what follows an affirmative changes what was agreed to."""
	return any(word in QUALIFIERS for word in rest)


def clear_pending(conversation) -> None:
	conversation.db_set(
		{"status": "Active", "pending_action": None, "pending_expires_on": None},
		update_modified=False,
	)


def park_for_confirmation(conversation, action_id: str, settings) -> None:
	minutes = settings.confirm_timeout_minutes or 10
	conversation.db_set(
		{
			"status": "Awaiting Confirmation",
			"pending_action": action_id,
			"pending_expires_on": add_to_date(now_datetime(), minutes=minutes),
		},
		update_modified=False,
	)


# ------------------------------------------------------------------ the loop


def run_agent(inbound, contact, conversation, settings, user, session, exclude_message=None) -> AgentResult:
	started = time.monotonic()

	run = frappe.get_doc(
		{
			"doctype": "Agent Run",
			"conversation": conversation.name,
			"contact": contact.name,
			"acting_user": user,
			"status": "Running",
			"prompt": inbound.get("message"),
			"provider": settings.ai_provider,
			"model": settings.ai_model,
		}
	)
	run.insert(ignore_permissions=True)
	# Commit now, so a tool that rolls back later cannot erase the record of
	# what was attempted.
	frappe.db.commit()

	ctx = ToolContext(settings, user, contact, conversation, run)
	tools = registry.build_schemas(settings) if settings.automation_enabled else []

	# Look up only what this message needs, rather than carrying the whole
	# knowledge base in every prompt.
	retrieved, hits = knowledge.context_for(inbound.get("message") or "", settings)
	corrections = load_corrections(settings)
	system = prompt.build(
		settings,
		contact,
		user,
		knowledge=retrieved,
		corrections=corrections,
		notes=memory.read(conversation),
	)

	turns = history(contact, settings, exclude_message)
	turns.append(user_turn(inbound, settings))

	trace: list[dict] = []
	usage = {"input_tokens": 0, "output_tokens": 0}
	reply_text = ""
	pending_action: str | None = None
	handed_over = False

	max_steps = settings.max_tool_iterations or 6

	try:
		for step in range(max_steps + 1):
			# The last pass offers no tools, forcing the model to actually answer
			# instead of looping until it runs out of steps.
			available = tools if step < max_steps else None

			answer = provider.complete(settings, system, turns, available)
			add_usage(usage, answer.usage)

			if not answer.wants_tools:
				reply_text = answer.text
				break

			turns.append(
				{"role": "assistant", "text": answer.text, "tool_calls": answer.tool_calls}
			)

			for call in answer.tool_calls:
				if call["name"] == "hand_over":
					handoff.start(
						conversation,
						settings,
						reason=(call.get("args") or {}).get("reason"),
					)
					handed_over = True

				result = registry.call(call["name"], call.get("args") or {}, ctx)
				trace.append({"step": step, "tool": call["name"], "args": call.get("args"), "result": summarise(result)})

				if result.get("status") == "awaiting_confirmation" and result.get("action_id"):
					pending_action = result["action_id"]

				turns.append(
					{
						"role": "tool",
						"id": call.get("id") or call["name"],
						"name": call["name"],
						"result": trim(result),
					}
				)
		else:
			reply_text = ""

	except Exception:
		traceback = frappe.get_traceback()

		# A tool that failed may have rolled the transaction back, taking this
		# run row with it. Recording why must not be what finally breaks.
		safe_set(
			run,
			{
				"status": "Failed",
				"error": traceback[:4000],
				"tool_calls": frappe.as_json(trace),
				"steps": len(trace),
				"duration_ms": elapsed(started),
			},
		)
		frappe.log_error(f"{traceback}\n\nAgent Run: {run.name}", "AgentX: agent run failed")

		fallback = (settings.fallback_reply or "").strip()
		return AgentResult(fallback or _("Sorry, something went wrong on my side. A person will get back to you."), run.name)

	# Handing over and asking for a confirmation both own the conversation
	# status, and the model can do both in one turn. Escalation wins: once a
	# person is involved they decide what happens to the pending change, and
	# asking the customer to confirm something a human is already reviewing
	# would be two voices again.
	if pending_action and not handed_over:
		park_for_confirmation(conversation, pending_action, settings)

		# The model wrote the conversation, but it must not be the source of the
		# numbers someone is agreeing to. Render those from the stored payload.
		confirmation = confirmation_text(pending_action)
		if confirmation:
			safe_set(run, {"reply": confirmation})
			conversation.db_set("last_message_on", now_datetime(), update_modified=False)
			return AgentResult(confirmation, run.name, pending=pending_action)

	if handed_over and not reply_text.strip():
		reply_text = (settings.handoff_message or "").strip() or _(
			"Let me get a person to help you. Someone will reply shortly."
		)

	if not reply_text.strip():
		reply_text = (settings.fallback_reply or "").strip() or _(
			"Sorry, I could not work that out. Let me get a person to help."
		)

	reply_text = clamp(reply_text, settings.max_reply_characters or 1500)

	safe_set(
		run,
		{
			"status": "Completed",
			"reply": reply_text,
			"tool_calls": frappe.as_json(trace),
			"steps": len(trace),
			"input_tokens": usage["input_tokens"],
			"output_tokens": usage["output_tokens"],
			"duration_ms": elapsed(started),
			"knowledge_used": frappe.as_json(
				[{"source": h["source"], "score": h["score"]} for h in hits]
			)
			if hits
			else None,
		},
	)

	conversation.db_set("last_message_on", now_datetime(), update_modified=False)

	if corrections:
		from agent_x.agentx.doctype.agent_correction.agent_correction import record_applied

		record_applied([c["name"] for c in corrections])

	return AgentResult(reply_text, run.name, pending=pending_action, handed_over=handed_over)


# ------------------------------------------------------------------ helpers


def load_corrections(settings) -> list[dict]:
	"""Lessons from replies that went wrong. Never blocks a reply."""
	if not settings.use_corrections:
		return []

	try:
		from agent_x.agentx.doctype.agent_correction.agent_correction import get_active

		return get_active(settings.correction_limit or 20)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "AgentX: could not load corrections")
		return []


def safe_set(doc, values: dict) -> None:
	"""Write to a document without letting the write become the failure.

	A tool that rolled the transaction back can leave this row gone. Losing the
	audit trail is bad; losing the reply because we could not write the audit
	trail is worse.
	"""
	try:
		doc.db_set(values, update_modified=False)
		frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "AgentX: could not update the agent run")


def confirmation_text(action_name: str) -> str | None:
	"""Render the pending change. Falls back to the model's words if it cannot."""
	try:
		action = frappe.get_doc("Agent Action", action_name)
		return summary.for_action(action)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "AgentX: could not render a confirmation")
		return None


def user_turn(inbound: dict, settings) -> dict:
	"""The incoming message, described so media is never read as silence."""
	text = (inbound.get("message") or "").strip()
	kind = (inbound.get("message_type") or "text").lower()

	if kind == "text":
		return {"role": "user", "text": text}

	# A transcribed voice note is just what they said.
	if inbound.get("transcribed") and text:
		return {"role": "user", "text": text}

	if inbound.get("transcription_failed"):
		return {
			"role": "user",
			"text": "[The customer sent a voice note that could not be transcribed. "
			"Apologise briefly and ask them to type it instead.]",
		}

	labels = {
		"image": "a photo",
		"video": "a video",
		"audio": "a voice note",
		"document": "a document",
		"sticker": "a sticker",
		"location": "their location",
		"contact": "a contact card",
	}
	described = f"[The person sent {labels.get(kind, 'a file')}"
	if inbound.get("media_filename"):
		described += f", file name: {inbound['media_filename']}"
	described += "]"

	body = f"{described}\nCaption: {text}" if text else f"{described}\nThere is no caption."

	turn = {"role": "user", "text": body}

	attachment = inbound.get("attachment") or {}
	kind_of_file = attachment.get("kind")

	if kind_of_file in ("image", "document") and attachment.get("data"):
		# Gemini takes both through the same inline part.
		turn[kind_of_file] = {
			"mime_type": attachment["mime_type"],
			"data": attachment["data"],
		}
		turn["text"] += (
			"\n[The file is attached. Read it. If it lists items to order, pull out each "
			"line with its quantity and look the codes up with match_items.]"
		)
	elif kind_of_file == "unreadable":
		turn["text"] += (
			f"\n[This is {attachment.get('label', 'a file')}, which you cannot open. "
			"Ask them to send it as a PDF or a photo instead.]"
		)
	elif kind_of_file in ("too_large", "unavailable"):
		turn["text"] += (
			"\n[The file could not be downloaded, possibly because it is too large. "
			"Ask them to send a smaller one or type the list out.]"
		)

	return turn


def history(contact, settings, exclude_message: str | None = None) -> list[dict]:
	"""Recent conversation, oldest first, as plain user and assistant turns.

	Tool calls are deliberately left out: replaying them would invite the model
	to repeat a write it already made.

	`exclude_message` drops the message being answered. The webhook logs it
	before the agent runs, so without this the model would see it twice: once as
	history and again as the new turn.
	"""
	limit = settings.history_limit or 12
	if limit <= 0:
		return []

	filters = {"contact": contact.name}
	if exclude_message:
		filters["name"] = ("!=", exclude_message)

	rows = frappe.get_all(
		"WhatsApp Message",
		filters=filters,
		fields=["direction", "message"],
		order_by="creation desc",
		limit=limit,
	)

	turns = [
		{"role": "assistant" if row.direction == "Outgoing" else "user", "text": (row.message or "").strip()}
		for row in reversed(rows)
		if (row.message or "").strip()
	]

	# Providers reject a conversation that opens with an assistant turn.
	while turns and turns[0]["role"] == "assistant":
		turns.pop(0)

	return turns


def add_usage(total: dict, usage: dict) -> None:
	for key in ("input_tokens", "output_tokens"):
		total[key] = (total[key] or 0) + (usage.get(key) or 0)


def trim(result: dict) -> dict:
	"""Keep a tool result small enough not to blow the context window."""
	text = frappe.as_json(result)
	if len(text) <= MAX_TOOL_RESULT_CHARS:
		return result

	return {
		"truncated": True,
		"note": _("The result was too large to show in full. Narrow the filters and try again."),
		"preview": text[:MAX_TOOL_RESULT_CHARS],
	}


def summarise(result: dict) -> dict:
	"""A compact version for the audit trail."""
	if not isinstance(result, dict):
		return {"result": str(result)[:500]}

	keep = {k: v for k, v in result.items() if k in ("status", "error", "refused", "action_id", "name", "count", "found", "doctype")}
	return keep or {"result": frappe.as_json(result)[:500]}


def clamp(text: str, limit: int) -> str:
	text = (text or "").strip()
	if len(text) <= limit:
		return text
	return text[:limit].rsplit(" ", 1)[0].rstrip() + "..."


def elapsed(started: float) -> int:
	return int((time.monotonic() - started) * 1000)
