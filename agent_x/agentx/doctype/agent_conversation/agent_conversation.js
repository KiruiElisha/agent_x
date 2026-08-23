const STATUS_COLOUR = {
	Active: "green",
	"Awaiting Confirmation": "orange",
	"Handed Over": "blue",
	Closed: "grey",
};

frappe.ui.form.on("Agent Conversation", {
	refresh(frm) {
		if (frm.doc.status) {
			frm.page.set_indicator(frm.doc.status, STATUS_COLOUR[frm.doc.status] || "grey");
		}
		if (frm.is_new()) return;

		if (frm.doc.status === "Handed Over") {
			frm.dashboard.set_headline(
				`<span class="indicator blue"></span>${__(
					"A person has this conversation. The assistant is not replying.",
				)}${
					frm.doc.handover_expires_on
						? ` ${__("It returns to the assistant at {0}.", [
								frappe.datetime.str_to_user(frm.doc.handover_expires_on),
							])}`
						: ""
				}`,
			);
			frm.add_custom_button(__("Give Back to Assistant"), () => run(frm, "give_back"));
		} else {
			frm.add_custom_button(__("Take Over"), () =>
				frappe.prompt(
					{ fieldname: "reason", fieldtype: "Small Text", label: __("Why (optional)") },
					(v) => run(frm, "take_over", { reason: v.reason }),
					__("Take Over"),
					__("Take Over"),
				),
			);
		}

		frm.add_custom_button(__("Reply"), () => reply(frm)).addClass("btn-primary");

		frm.trigger("render_thread");
	},

	render_thread(frm) {
		frm.call({ doc: frm.doc, method: "thread" }).then((r) => {
			const messages = r.message || [];
			if (!messages.length) return;

			const html = messages
				.map((m) => {
					const incoming = m.direction === "Incoming";
					const body = frappe.utils.escape_html(m.message || `(${m.message_type})`);
					const when = frappe.datetime.str_to_user(m.creation);
					const tag = m.alert ? ` · ${__("alert")}` : "";

					return `
						<div style="display:flex;margin-bottom:8px;
						            justify-content:${incoming ? "flex-start" : "flex-end"};">
							<div style="max-width:74%;padding:8px 12px;border-radius:12px;
							            background:${incoming ? "var(--bg-light-gray)" : "var(--bg-blue)"};
							            white-space:pre-wrap;font-size:13px;line-height:1.45;">
								${body}
								<div class="text-muted" style="font-size:11px;margin-top:4px;">
									${when} · ${frappe.utils.escape_html(m.status || "")}${tag}
								</div>
							</div>
						</div>`;
				})
				.join("");

			frm.dashboard.add_section(
				`<div style="max-height:420px;overflow-y:auto;padding:8px 4px;">${html}</div>`,
				__("Conversation"),
			);
		});
	},
});

function run(frm, method, args) {
	return frm
		.call({ doc: frm.doc, method, args: args || {}, freeze: true })
		.then(() => frm.reload_doc());
}

function reply(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Reply"),
		fields: [
			{
				fieldname: "note",
				fieldtype: "HTML",
				options:
					frm.doc.status === "Handed Over"
						? ""
						: `<div class="text-muted" style="margin-bottom:8px;">${__(
								"Replying takes the conversation over, so the assistant stops answering.",
							)}</div>`,
			},
			{ fieldname: "message", fieldtype: "Small Text", label: __("Message"), reqd: 1 },
		],
		primary_action_label: __("Send"),
		primary_action(values) {
			frm.call({
				doc: frm.doc,
				method: "send_reply",
				args: { message: values.message },
				freeze: true,
				freeze_message: __("Sending…"),
			}).then(() => {
				dialog.hide();
				frappe.show_alert({ message: __("Sent"), indicator: "green" });
				frm.reload_doc();
			});
		},
	});
	dialog.show();
}
