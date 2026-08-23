frappe.ui.form.on("WhatsApp Alert", {
	refresh(frm) {
		frm.trigger("render_condition_help");

		if (frm.is_new()) return;

		frm.add_custom_button(__("Preview"), () => preview(frm));
		frm.add_custom_button(__("Send a Test"), () => send_test(frm));

		if (frm.doc.last_error) {
			frm.dashboard.set_headline(
				`<span class="indicator red"></span>${__("Last failure")}: ${frappe.utils.escape_html(
					frm.doc.last_error,
				)}`,
			);
		}
	},

	document_type(frm) {
		frm.trigger("render_condition_help");
	},

	render_condition_help(frm) {
		const wrapper = frm.get_field("condition_help")?.$wrapper;
		if (!wrapper) return;

		const dt = frm.doc.document_type || "Sales Order";
		wrapper.empty().append(`
			<div class="text-muted small" style="line-height:1.6;">
				${__("Examples")}:
				<code>doc.grand_total &gt; 10000</code>,
				<code>doc.status == "Overdue"</code>,
				<code>doc.customer_group == "Retail"</code>
				<br>${__("The document is available as {0}. Leave empty to send every time.", [
					`<code>doc</code>`,
				])}
				<br>${__("Fields come from")} <a href="/app/doctype/${encodeURIComponent(dt)}">${frappe.utils.escape_html(dt)}</a>.
			</div>
		`);
	},
});

function pick_document(frm, title, onpick) {
	const dialog = new frappe.ui.Dialog({
		title,
		fields: [
			{
				fieldname: "docname",
				fieldtype: "Link",
				options: frm.doc.document_type,
				label: frm.doc.document_type,
				reqd: 1,
			},
			{ fieldname: "output", fieldtype: "HTML" },
		],
		primary_action_label: __("Go"),
		primary_action(values) {
			onpick(values.docname, dialog);
		},
	});
	dialog.show();
	return dialog;
}

function preview(frm) {
	pick_document(frm, __("Preview Against a Document"), (docname, dialog) => {
		frm.call({
			doc: frm.doc,
			method: "preview",
			args: { docname },
			freeze: true,
			freeze_message: __("Rendering…"),
		}).then((r) => {
			const result = r.message || {};
			const wrapper = dialog.fields_dict.output.$wrapper;

			if (!result.ok) {
				wrapper
					.empty()
					.append(`<div class="text-danger">${frappe.utils.escape_html(result.error)}</div>`);
				return;
			}

			const rows = [
				[__("To"), result.number || `<span class="text-danger">${__("no number found")}</span>`],
				[
					__("Condition"),
					result.passes_condition
						? `<span class="indicator green"></span>${__("passes")}`
						: `<span class="indicator red"></span>${__("does not pass, so nothing would send")}`,
				],
			];

			if (result.already_sent) {
				rows.push([__("Note"), __("Already sent for this document; it would not send again.")]);
			}

			wrapper.empty().append(`
				<div style="margin-top:8px;">
					${rows
						.map(
							([k, v]) =>
								`<div style="margin-bottom:4px;"><b>${k}:</b> ${v}</div>`,
						)
						.join("")}
					<div style="margin-top:10px;padding:12px;border-radius:8px;
					            background:var(--bg-light-gray);white-space:pre-wrap;">
						${frappe.utils.escape_html(result.message)}
					</div>
				</div>
			`);
		});
	});
}

function send_test(frm) {
	pick_document(frm, __("Send a Real Message"), (docname, dialog) => {
		frappe.confirm(
			__("This sends a real WhatsApp message to the number on {0}. Continue?", [docname]),
			() => {
				frm.call({
					doc: frm.doc,
					method: "send_now",
					args: { docname },
					freeze: true,
					freeze_message: __("Sending…"),
				}).then((r) => {
					const result = r.message || {};
					dialog.hide();
					frappe.msgprint({
						title: result.sent ? __("Sent") : __("Not sent"),
						message: result.sent
							? __("Delivered to {0}.", [frappe.utils.escape_html(result.to || "")])
							: frappe.utils.escape_html(result.reason || __("Check the alert's last error.")),
						indicator: result.sent ? "green" : "orange",
					});
					frm.reload_doc();
				});
			},
		);
	});
}
