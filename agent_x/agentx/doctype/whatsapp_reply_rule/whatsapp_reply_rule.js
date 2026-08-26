frappe.ui.form.on("WhatsApp Reply Rule", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Try a Message"), () => try_message(frm));

		if (frm.doc.times_used) {
			frm.dashboard.set_headline(
				__("Answered {0} messages without the model, saving roughly {1} tokens.", [
					frappe.format(frm.doc.times_used, { fieldtype: "Int" }),
					frappe.format(frm.doc.tokens_saved, { fieldtype: "Int" }),
				]),
			);
		}
	},
});

function try_message(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Would this rule answer?"),
		fields: [
			{ fieldname: "text", fieldtype: "Data", label: __("A message a customer might send"), reqd: 1 },
			{ fieldname: "result", fieldtype: "HTML" },
		],
		primary_action_label: __("Check"),
		primary_action(values) {
			frm.call({ doc: frm.doc, method: "test_match", args: { text: values.text } }).then((r) => {
				const out = r.message || {};
				dialog.fields_dict.result.$wrapper.empty().append(
					out.matched
						? `<div><span class="indicator green"></span>${__("Matches")}</div>
						   <div style="margin-top:8px;padding:10px;border-radius:8px;
						               background:var(--bg-light-gray);white-space:pre-wrap;">
						     ${frappe.utils.escape_html(out.reply || "")}
						   </div>`
						: `<div><span class="indicator grey"></span>${__(
								"No match, so this one would go to the assistant.",
							)}</div>`,
				);
			});
		},
	});
	dialog.show();
}
