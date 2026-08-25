"""The assistant's working memory for one conversation.

A customer asks for nine items, and answering takes a detour: no account
exists, so a customer has to be created and confirmed first. By the time that
finishes, the original request may have fallen out of the message window, or
be buried far enough back that the model treats "now my order" as a new and
meaningless sentence.

Conversation history alone cannot fix this. It is a window, and it slides. A
note is deliberate: the assistant writes down what it still owes somebody, and
that note is put in front of it on every turn until it says the work is done.
"""

import frappe
from frappe import _

MAX_NOTE = 2000


def read(conversation) -> str:
	if not conversation:
		return ""
	return (conversation.notes or "").strip()


def remember(ctx, note: str) -> dict:
	"""Write down something that must survive the rest of the conversation."""
	if not ctx.conversation:
		return {"ok": False, "error": _("There is no conversation to remember this against.")}

	text = (note or "").strip()
	if not text:
		return {"ok": False, "error": _("Say what to remember.")}

	ctx.conversation.db_set("notes", text[:MAX_NOTE], update_modified=False)
	frappe.db.commit()

	return {
		"ok": True,
		"remembered": text[:MAX_NOTE],
		"note": _("This is now in front of you on every reply until you call forget."),
	}


def forget(ctx) -> dict:
	"""Drop the note once the work behind it is finished."""
	if not ctx.conversation:
		return {"ok": True}

	ctx.conversation.db_set("notes", None, update_modified=False)
	frappe.db.commit()

	return {"ok": True, "note": _("Cleared.")}
