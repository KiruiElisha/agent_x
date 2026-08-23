/** Flattens Baileys message objects into the shape Frappe stores. */

const TEXT_KEYS = ["conversation", "extendedTextMessage"];

const MEDIA_KINDS = {
	imageMessage: "image",
	videoMessage: "video",
	audioMessage: "audio",
	documentMessage: "document",
	stickerMessage: "sticker",
	documentWithCaptionMessage: "document",
};

/** Unwrap the layers WhatsApp wraps a real message in (view-once, ephemeral, edits). */
export function unwrap(message) {
	let current = message;

	for (let depth = 0; depth < 5 && current; depth++) {
		const inner =
			current.ephemeralMessage?.message ||
			current.viewOnceMessage?.message ||
			current.viewOnceMessageV2?.message ||
			current.viewOnceMessageV2Extension?.message ||
			current.documentWithCaptionMessage?.message ||
			current.editedMessage?.message;

		if (!inner) break;
		current = inner;
	}

	return current || {};
}

export function extractText(message) {
	const inner = unwrap(message);

	if (typeof inner.conversation === "string") return inner.conversation;
	if (inner.extendedTextMessage?.text) return inner.extendedTextMessage.text;

	// Captions read as the message body for media.
	for (const key of Object.keys(MEDIA_KINDS)) {
		if (inner[key]?.caption) return inner[key].caption;
	}

	// Interactive replies carry the choice the user tapped.
	if (inner.buttonsResponseMessage?.selectedDisplayText) {
		return inner.buttonsResponseMessage.selectedDisplayText;
	}
	if (inner.listResponseMessage?.title) return inner.listResponseMessage.title;
	if (inner.templateButtonReplyMessage?.selectedDisplayText) {
		return inner.templateButtonReplyMessage.selectedDisplayText;
	}
	if (inner.reactionMessage?.text) return inner.reactionMessage.text;

	return "";
}

export function describeMedia(message) {
	const inner = unwrap(message);

	for (const [key, kind] of Object.entries(MEDIA_KINDS)) {
		const node = inner[key];
		if (!node) continue;

		return {
			type: kind,
			mimetype: node.mimetype || null,
			filename: node.fileName || null,
			// Size arrives as a Long; make it a plain number for JSON.
			size: node.fileLength ? Number(node.fileLength) : null,
			seconds: node.seconds ?? null,
			// The bytes are only reachable through Baileys' decryptor, so the
			// message key is what Frappe needs to ask for a download later.
			downloadable: true,
		};
	}

	if (inner.locationMessage) {
		return {
			type: "location",
			latitude: inner.locationMessage.degreesLatitude,
			longitude: inner.locationMessage.degreesLongitude,
			downloadable: false,
		};
	}

	if (inner.contactMessage || inner.contactsArrayMessage) {
		return { type: "contact", downloadable: false };
	}

	return null;
}

export function messageType(message) {
	const media = describeMedia(message);
	if (media) return media.type;
	return "text";
}

const VOICE_KINDS = new Set(["audio", "voice", "ptt"]);

/** Whether this message is something the assistant should try to transcribe. */
export function isVoice(event) {
	return VOICE_KINDS.has(event?.media?.type) || VOICE_KINDS.has(event?.message_type);
}

/** Digits-only sender id, e.g. "254712345678" from "254712345678@s.whatsapp.net". */
export function jidToNumber(jid) {
	if (!jid) return null;
	const [user] = String(jid).split("@");
	// Multi-device jids look like 2547...:12@s.whatsapp.net.
	return (user || "").split(":")[0] || null;
}

export function isGroup(jid) {
	return String(jid || "").endsWith("@g.us");
}

/** Build the event body posted to Frappe for one inbound message. */
export function normalise(sessionId, raw) {
	const key = raw.key || {};
	const chatId = key.remoteJid;
	const group = isGroup(chatId);

	// In a group the author is participant; in a direct chat it is the chat itself.
	const senderJid = group ? key.participant || raw.participant : chatId;

	return {
		session: sessionId,
		message_id: key.id || null,
		chat_id: chatId || null,
		from_me: Boolean(key.fromMe),
		is_group: group,
		wa_id: jidToNumber(senderJid),
		group_id: group ? jidToNumber(chatId) : null,
		push_name: raw.pushName || null,
		message_type: messageType(raw.message),
		text: extractText(raw.message),
		media: describeMedia(raw.message),
		// Baileys gives seconds; Frappe wants something it can parse.
		timestamp: raw.messageTimestamp ? Number(raw.messageTimestamp) : null,
	};
}
