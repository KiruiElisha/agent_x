frappe.ui.form.on("Bridge Subscription", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Collect payment"), () => {
			frappe.call({
				method: "agent_x.billing.collect",
				args: { email: frm.doc.email, phone: frm.doc.phone },
				freeze: true,
				callback(result) {
					const message = result.message || {};
					if (message.setup_url) {
						window.open(message.setup_url, "_blank", "noopener");
					}
					frappe.msgprint(message.invoice_id
						? __("M-Pesa prompt sent ({0}).", [message.invoice_id])
						: __("Payment started."));
					frm.reload_doc();
				},
			});
		});
	},
});
