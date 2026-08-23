frappe.ui.form.on("Agent Run", {
	refresh(frm) {
		if (frm.is_new()) return;

		// The cheapest moment to teach the assistant something is right after
		// you have read a reply that was wrong.
		frm.add_custom_button(__("This Reply Was Wrong"), () => record_correction(frm));

		if (frm.doc.conversation) {
			frm.add_custom_button(__("Open Conversation"), () =>
				frappe.set_route("Form", "Agent Conversation", frm.doc.conversation),
			);
		}
	},
});

function record_correction(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Record a Correction"),
		fields: [
			{
				fieldname: "intro",
				fieldtype: "HTML",
				options: `<div class="text-muted" style="margin-bottom:10px;line-height:1.5;">${__(
					"This becomes a do-not-repeat instruction in every future reply. Keep it specific.",
				)}</div>`,
			},
			{
				fieldname: "applies_when",
				fieldtype: "Small Text",
				label: __("When does this apply?"),
				reqd: 1,
				default: frm.doc.prompt || "",
				description: __("The situation, e.g. 'someone asks about refunds on sale items'."),
			},
			{
				fieldname: "wrong_reply",
				fieldtype: "Small Text",
				label: __("What it wrongly said"),
				default: frm.doc.reply || "",
			},
			{
				fieldname: "correct_behaviour",
				fieldtype: "Small Text",
				label: __("What it should do instead"),
				reqd: 1,
			},
		],
		primary_action_label: __("Save Correction"),
		primary_action(values) {
			frappe.call({
				method: "agent_x.api.record_correction",
				args: {
					agent_run: frm.doc.name,
					applies_when: values.applies_when,
					wrong_reply: values.wrong_reply,
					correct_behaviour: values.correct_behaviour,
				},
				freeze: true,
				callback(r) {
					dialog.hide();
					frappe.show_alert({ message: __("Correction saved"), indicator: "green" });
					if (r.message?.name) {
						frappe.set_route("Form", "Agent Correction", r.message.name);
					}
				},
			});
		},
	});
	dialog.show();
}
