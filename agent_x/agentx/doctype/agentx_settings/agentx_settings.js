frappe.ui.form.on("AgentX Settings", {
	refresh(frm) {
		frm.trigger("render_policy_help");
		frm.trigger("render_bridge_status");
		frm.trigger("render_provider_help");
		frm.trigger("render_all_warning");
		frm.trigger("render_connection");
		frm.trigger("listen_for_pairing");

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

	render_connection(frm) {
		const wrapper = frm.get_field("connection_html")?.$wrapper;
		if (!wrapper) return;

		wrapper.empty().append(
			`<div class="text-muted">${__("Checking…")}</div>`,
		);

		frm.call({ doc: frm.doc, method: "connection_state" })
			.then((r) => paint(frm, r.message || {}))
			.catch(() => paint(frm, { configured: false, state: "Unavailable" }));
	},

	listen_for_pairing(frm) {
		if (frm.__agentx_listening) return;
		frm.__agentx_listening = true;

		// The QR is pushed from the webhook, so nothing here polls.
		frappe.realtime.on("agentx_session_update", (data) => {
			if (!data) return;
			paint(frm, {
				configured: true,
				session: data.session,
				state: data.state,
				qr: data.qr,
				phone: data.phone,
				error: data.error,
			});
			if (data.event === "connected") {
				frappe.show_alert({ message: __("WhatsApp connected"), indicator: "green" });
			}
		});
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
		const wrapper = frm.get_field("connection_html").$wrapper;
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

function paint(frm, state) {
	const wrapper = frm.get_field("connection_html")?.$wrapper;
	if (!wrapper) return;

	const box = (body, indicator) =>
		`<div style="padding:14px;border:1px solid var(--border-color);border-radius:8px;">
			${indicator ? `<span class="indicator ${indicator}"></span>` : ""}${body}
		 </div>`;

	if (!state.configured) {
		wrapper
			.empty()
			.append(
				box(
					`${__("Not set up yet.")} <span class="text-muted">${__(
						"Fill in the Instance ID and Access Token above, save, then press Connect WhatsApp.",
					)}</span>`,
				),
			);
		return;
	}

	if (state.state === "Connected") {
		wrapper.empty().append(
			box(
				`<b>${__("Connected")}</b>
				 <div class="text-muted" style="margin-top:4px;">
					${state.phone ? "+" + frappe.utils.escape_html(state.phone) : __("number unknown")}
				 </div>`,
				"green",
			),
		);
		return;
	}

	if (state.qr) {
		wrapper.empty().append(`
			<div style="text-align:center;padding:12px 0;">
				<img src="${frappe.utils.escape_html(state.qr)}" alt="${__("WhatsApp QR code")}"
				     style="width:240px;height:240px;image-rendering:pixelated;background:#fff;
				            border:1px solid var(--border-color);border-radius:8px;padding:8px;" />
				<div class="text-muted" style="margin-top:10px;max-width:340px;margin-inline:auto;line-height:1.5;">
					${__("On your phone open WhatsApp, go to Linked Devices, and scan this code.")}
					<br>${__("It refreshes by itself until it is scanned.")}
				</div>
			</div>
		`);
		return;
	}

	const detail = state.error
		? `<div class="text-muted" style="margin-top:4px;">${frappe.utils.escape_html(state.error)}</div>`
		: `<div class="text-muted" style="margin-top:4px;">${__("Press Connect WhatsApp to pair a phone.")}</div>`;

	wrapper
		.empty()
		.append(box(`<b>${frappe.utils.escape_html(state.state || __("Not connected"))}</b>${detail}`,
			state.state === "Pairing" ? "orange" : "red"));
}

function connect(frm) {
	if (frm.is_dirty()) {
		frappe.msgprint({
			title: __("Save first"),
			message: __("Save the settings, then press Connect WhatsApp."),
			indicator: "orange",
		});
		return;
	}

	frm.call({
		doc: frm.doc,
		method: "connect_whatsapp",
		freeze: true,
		freeze_message: __("Starting…"),
	}).then((r) => {
		const result = r.message || {};
		frappe.show_alert({
			message: __("Pairing started on session {0}", [result.session || ""]),
			indicator: "blue",
		});
		frm.trigger("render_connection");
	});
}

function diagnose() {
	frappe.call({
		method: "agent_x.diagnostics.run",
		freeze: true,
		freeze_message: __("Checking every step…"),
		callback(r) {
			const result = r.message || {};
			const rows = (result.results || [])
				.map((c) => {
					const mark =
						c.ok === true
							? '<span class="indicator green"></span>'
							: c.ok === false
								? '<span class="indicator red"></span>'
								: '<span class="indicator grey"></span>';
					const detail = c.detail
						? `<div class="text-muted small">${frappe.utils.escape_html(c.detail)}</div>`
						: "";
					const fix =
						c.ok === false && c.fix
							? `<div class="small" style="margin-top:2px;"><b>${__("Fix")}:</b> ${frappe.utils.escape_html(c.fix)}</div>`
							: "";
					return `<div style="padding:7px 0;border-bottom:1px solid var(--border-color);">
								${mark}${frappe.utils.escape_html(c.check)}${detail}${fix}
							</div>`;
				})
				.join("");

			frappe.msgprint({
				title: __("AgentX Diagnosis"),
				indicator: result.ok ? "green" : "red",
				message: `<div style="margin-bottom:8px;">${frappe.utils.escape_html(result.summary || "")}</div>${rows}`,
				wide: true,
			});
		},
	});
}
