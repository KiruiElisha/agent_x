const STATUS_COLOUR = { Ready: "green", Building: "orange", Pending: "grey", Failed: "red" };

frappe.ui.form.on("Agent Knowledge", {
	refresh(frm) {
		if (frm.doc.status) {
			frm.page.set_indicator(frm.doc.status, STATUS_COLOUR[frm.doc.status] || "grey");
		}

		if (frm.is_new()) return;

		frm.add_custom_button(__("Rebuild Index"), () =>
			frm
				.call({ doc: frm.doc, method: "rebuild", freeze: true, freeze_message: __("Indexing…") })
				.then((r) => {
					const result = r.message || {};
					frappe.show_alert({
						message: __("Indexed into {0} chunks", [result.chunks || 0]),
						indicator: "green",
					});
					frm.reload_doc();
				}),
		);

		if (frm.doc.status === "Ready") {
			frm.add_custom_button(__("Test a Question"), () => preview(frm));
		}

		if (frm.doc.status === "Pending") {
			frm.dashboard.set_headline(
				__("Not indexed yet. It will be picked up shortly, or press Rebuild Index."),
			);
		}
	},
});

function preview(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("What would this retrieve?"),
		fields: [
			{
				fieldname: "query",
				fieldtype: "Data",
				label: __("A question a customer might ask"),
				reqd: 1,
			},
			{ fieldname: "results", fieldtype: "HTML" },
		],
		primary_action_label: __("Search"),
		primary_action(values) {
			frm.call({
				doc: frm.doc,
				method: "preview_search",
				args: { query: values.query },
				freeze: true,
				freeze_message: __("Searching…"),
			}).then((r) => {
				const hits = (r.message || {}).hits || [];
				const wrapper = dialog.fields_dict.results.$wrapper;

				if (!hits.length) {
					wrapper
						.empty()
						.append(
							`<div class="text-muted">${__(
								"Nothing matched closely enough. Lower the minimum similarity, or add content covering this.",
							)}</div>`,
						);
					return;
				}

				wrapper.empty().append(
					hits
						.map(
							(h) => `
						<div style="border:1px solid var(--border-color);border-radius:6px;
						            padding:10px;margin-bottom:8px;">
							<div class="text-muted small" style="margin-bottom:6px;">
								${frappe.utils.escape_html(h.source)} — ${__("score")} ${h.score}
							</div>
							<div style="white-space:pre-wrap;font-size:13px;">
								${frappe.utils.escape_html(h.content).slice(0, 600)}
							</div>
						</div>`,
						)
						.join(""),
				);
			});
		},
	});
	dialog.show();
}
