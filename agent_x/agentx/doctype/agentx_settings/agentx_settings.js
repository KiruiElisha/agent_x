frappe.ui.form.on("AgentX Settings", {
	refresh(frm) {
		frm.trigger("render_policy_help");
		frm.trigger("render_bridge_status");
		frm.trigger("render_provider_help");
		frm.trigger("render_all_warning");

		frm.add_custom_button(__("Test Connection"), () => test_connection(frm));
		frm.add_custom_button(__("Test AI"), () => test_ai(frm));
		frm.add_custom_button(__("Ask the Assistant"), () => ask_agent());

		frm.add_custom_button(__("Sessions"), () =>
			frappe.set_route("List", "WhatsApp Session"),
		);

		if (frm.doc.whatsapp_provider === "WaClient") {
			frm.add_custom_button(__("Register Webhook"), () => register_webhook(frm));
		}

		if (frm.doc.webhook_url) {
			frm.set_df_property(
				"webhook_url",
				"description",
				frm.doc.whatsapp_provider === "Self-Hosted Bridge"
					? __("Set this as BRIDGE_WEBHOOK_URL in the bridge environment, then restart the bridge.")
					: __("Press Register Webhook to point WaClient at this URL."),
			);
		}
	},

	automation_enabled(frm) {
		if (frm.doc.automation_enabled && !frm.doc.doctype_policies?.length) {
			frappe.msgprint({
				title: __("Add Document Policies"),
				indicator: "orange",
				message: __(
					"Document automation is on, but the assistant can only touch document types you list below.",
				),
			});
		}
	},

	policy_mode(frm) {
		frm.trigger("render_all_warning");
	},

	render_all_warning(frm) {
		const wrapper = frm.get_field("all_documents_warning")?.$wrapper;
		if (!wrapper) return;

		wrapper.empty();
		if (frm.doc.policy_mode !== "All Documents") return;

		wrapper.append(`
			<div style="border-left:3px solid var(--red-400);padding:10px 14px;
			            background:var(--bg-light-gray);border-radius:4px;line-height:1.6;">
				<b>${__("This is a much larger surface.")}</b><br>
				${__("The assistant can reach any document type the acting user can, not just the ones you list.")}
				${__("Two things still hold it back: a fixed list of access and secret bearing doctypes it can never touch, and the permissions of the user each number acts as.")}
				<br><br>
				${__("Keep Approval Required on, and give the mapped users the narrowest roles that still do the job.")}
			</div>
		`);
	},

	render_policy_help(frm) {
		const wrapper = frm.get_field("policy_help")?.$wrapper;
		if (!wrapper) return;

		wrapper.empty().append(`
			<div class="text-muted" style="line-height:1.6;margin-bottom:8px;">
				${__("The assistant can only use document types listed here, and only the operations you tick.")}
				${__("On top of that, every action is checked against the permissions of the user the number acts as.")}
				<br>
				<b>${__("Approval Required")}</b> ${__("makes the assistant describe the change and wait for a YES before anything is written.")}
			</div>
		`);
	},

	render_bridge_status(frm) {
		const wrapper = frm.get_field("bridge_status_html")?.$wrapper;
		if (!wrapper) return;

		wrapper.empty().append(
			`<div class="text-muted">${__("Press Test Connection to check the provider.")}</div>`,
		);
	},

	whatsapp_provider(frm) {
		frm.trigger("render_provider_help");
		frm.trigger("render_bridge_status");
	},

	render_provider_help(frm) {
		const wrapper = frm.get_field("provider_help")?.$wrapper;
		if (!wrapper) return;

		const hosted =
			__("WaClient hosts the WhatsApp session for you, so AgentX only makes HTTP calls.") +
			" " +
			__("This is the option that works on Frappe Cloud, where nothing long-running can be installed.") +
			" " +
			__("The trade is that WaClient holds the session and can read the messages.");

		const own =
			__("The bridge in this repo runs WhatsApp Web yourself, so no third party sees your messages.") +
			" " +
			__("It needs a machine where a Node process can stay running, which rules out Frappe Cloud.");

		wrapper
			.empty()
			.append(
				`<div class="text-muted" style="line-height:1.6;">${
					frm.doc.whatsapp_provider === "Self-Hosted Bridge" ? own : hosted
				}</div>`,
			);
	},
});

function test_connection(frm) {
	frm.call({
		doc: frm.doc,
		method: "test_connection",
		freeze: true,
		freeze_message: __("Checking…"),
	}).then((r) => {
		const result = r.message || {};
		const wrapper = frm.get_field("bridge_status_html").$wrapper;
		const provider = frappe.utils.escape_html(result.provider || "");

		if (!result.reachable) {
			wrapper
				.empty()
				.append(
					`<div><span class="indicator red"></span>${__("Cannot reach {0}", [provider])}</div>
					 <div class="text-muted small">${frappe.utils.escape_html(result.error || "")}</div>`,
				);
			return;
		}

		const bits = [`<div><span class="indicator green"></span>${__("{0} reachable", [provider])}</div>`];

		if (result.state) {
			const colour = result.state === "connected" ? "green" : "orange";
			bits.push(
				`<div class="small"><span class="indicator ${colour}"></span>${__("Session is {0}", [
					frappe.utils.escape_html(result.state),
				])}${result.phone ? ` (+${frappe.utils.escape_html(result.phone)})` : ""}</div>`,
			);
		}

		if (Array.isArray(result.sessions)) {
			bits.push(
				result.sessions.length
					? result.sessions
							.map(
								(s) =>
									`<div class="small">${frappe.utils.escape_html(s.session)} — ${frappe.utils.escape_html(
										s.state,
									)}</div>`,
							)
							.join("")
					: `<div class="text-muted small">${__("No sessions started yet.")}</div>`,
			);
		}

		wrapper.empty().append(bits.join(""));
	});
}

function register_webhook(frm) {
	frm.call({
		doc: frm.doc,
		method: "register_webhook",
		freeze: true,
		freeze_message: __("Registering…"),
	}).then((r) => {
		const result = r.message || {};

		if (!result.supported) {
			frappe.msgprint({
				title: __("Nothing to do"),
				message: result.note || __("This provider is told its webhook somewhere else."),
				indicator: "blue",
			});
			return;
		}

		frappe.msgprint({
			title: result.verified ? __("Webhook registered") : __("Webhook may not be active"),
			message: result.verified
				? __("The provider confirmed it will post to {0}.", [
						frappe.utils.escape_html(result.webhook_url),
					])
				: __("The provider accepted the URL but did not confirm it. Registered: {0}", [
						frappe.utils.escape_html(result.registered_url || "none"),
					]),
			indicator: result.verified ? "green" : "orange",
		});
	});
}

function test_ai(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Test AI"),
		fields: [
			{
				fieldname: "message",
				fieldtype: "Small Text",
				label: __("Message"),
				reqd: 1,
				default: __("Hello, are you there?"),
			},
		],
		primary_action_label: __("Send"),
		primary_action(values) {
			frm.call({
				doc: frm.doc,
				method: "test_ai",
				args: { message: values.message },
				freeze: true,
				freeze_message: __("Asking the model…"),
			}).then((r) => {
				dialog.hide();
				const result = r.message || {};
				frappe.msgprint({
					title: __("{0} replied", [result.provider || __("The model")]),
					message: `<div style="white-space:pre-wrap;">${frappe.utils.escape_html(result.reply || "")}</div>`,
					indicator: "green",
				});
			});
		},
	});
	dialog.show();
}

function ask_agent() {
	const dialog = new frappe.ui.Dialog({
		title: __("Ask the Assistant"),
		fields: [
			{
				fieldname: "warning",
				fieldtype: "HTML",
				options: `<div class="text-muted" style="margin-bottom:8px;">${__(
					"The assistant runs for real. Any document action it decides on will actually happen, as your user.",
				)}</div>`,
			},
			{ fieldname: "message", fieldtype: "Small Text", label: __("Message"), reqd: 1 },
		],
		primary_action_label: __("Ask"),
		primary_action(values) {
			frappe.call({
				method: "agent_x.api.ask",
				args: { message: values.message },
				freeze: true,
				freeze_message: __("Thinking…"),
				callback(r) {
					dialog.hide();
					const result = r.message || {};
					frappe.msgprint({
						title: __("Assistant"),
						message:
							`<div style="white-space:pre-wrap;">${frappe.utils.escape_html(result.reply || "")}</div>` +
							(result.run
								? `<div class="text-muted small" style="margin-top:8px;">
								     <a href="/app/agent-run/${encodeURIComponent(result.run)}">${__("See what it did")}</a>
								   </div>`
								: ""),
						indicator: "blue",
					});
				},
			});
		},
	});
	dialog.show();
}
