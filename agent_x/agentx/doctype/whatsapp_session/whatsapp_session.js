// Pairing UI. The QR arrives over realtime, so nothing here polls.

const STATE_COLOUR = {
	Connected: "green",
	Pairing: "orange",
	Disconnected: "red",
	"Logged Out": "red",
};

frappe.ui.form.on("WhatsApp Session", {
	refresh(frm) {
		frm.trigger("render_pairing");
		frm.trigger("set_buttons");
		frm.trigger("listen");
		frm.trigger("watch_pairing");

		if (frm.doc.state) {
			frm.page.set_indicator(frm.doc.state, STATE_COLOUR[frm.doc.state] || "grey");
		}
	},

	// WaClient pushes nothing, so while a QR is on screen we ask for the state
	// ourselves. The bridge does push, so it never needs this.
	watch_pairing(frm) {
		clearInterval(frm.__agentx_poll);
		frm.__agentx_poll = null;

		if (frm.is_new()) return;
		if (frm.doc.provider === "Self-Hosted Bridge") return;
		if (frm.doc.state !== "Pairing") return;

		frm.__agentx_poll = setInterval(() => {
			// Stop as soon as the form is gone, or we would poll forever.
			if (cur_frm !== frm || frm.doc.state !== "Pairing") {
				clearInterval(frm.__agentx_poll);
				frm.__agentx_poll = null;
				return;
			}

			frm.call({ doc: frm.doc, method: "refresh_status" })
				.then((r) => {
					const state = (r.message || {}).state;
					if (state === "connected") {
						clearInterval(frm.__agentx_poll);
						frm.__agentx_poll = null;
						frappe.show_alert({ message: __("WhatsApp connected"), indicator: "green" });
						frm.reload_doc();
					}
				})
				.catch(() => {
					clearInterval(frm.__agentx_poll);
					frm.__agentx_poll = null;
				});
		}, 5000);
	},

	onload(frm) {
		frm.trigger("listen");

		// A new session has no provider yet, and the Instance ID field keys off
		// it. Take both from settings so the form is usable straight away.
		if (frm.is_new()) {
			frappe.db.get_doc("AgentX Settings").then((settings) => {
				if (!frm.doc.provider) {
					frm.set_value("provider", settings.whatsapp_provider || "WaClient");
				}
				if (!frm.doc.instance_id && settings.waclient_instance_id) {
					frm.set_value("instance_id", settings.waclient_instance_id);
				}
				if (!frm.doc.session_name) {
					frm.set_value("session_name", "main");
				}
			});
		}
	},

	listen(frm) {
		if (frm.__agentx_listening) return;
		frm.__agentx_listening = true;

		frappe.realtime.on("agentx_session_update", (data) => {
			if (!data || data.session !== frm.doc.name) return;

			// Keep the in-memory doc in step with what the bridge just told us,
			// so a later save does not write a stale state back.
			if (data.state) frm.doc.state = data.state;
			if (data.phone) frm.doc.phone_number = data.phone;
			if ("qr" in data) frm.doc.qr_data = data.qr;
			frm.doc.last_error = data.error || null;

			frm.trigger("render_pairing");
			frm.trigger("set_buttons");
			frm.page.set_indicator(frm.doc.state, STATE_COLOUR[frm.doc.state] || "grey");
			frm.refresh_field("phone_number");

			if (data.event === "connected") {
				frappe.show_alert({ message: __("WhatsApp connected"), indicator: "green" });
			}
			if (data.event === "logged_out") {
				frappe.show_alert({ message: __("WhatsApp session logged out"), indicator: "red" });
			}
		});
	},

	set_buttons(frm) {
		frm.clear_custom_buttons();
		if (frm.is_new()) return;

		const connected = frm.doc.state === "Connected";

		if (!connected) {
			frm.add_custom_button(__("Connect"), () => run(frm, "connect", __("Starting…")));

			if (frm.doc.provider === "WaClient" && !frm.doc.instance_id) {
				frm.add_custom_button(__("Create Instance"), () =>
					run(frm, "create_instance", __("Creating…")),
				);
			}

			// Scanning is awkward on a desktop-only WhatsApp, so offer the code path.
			frm.add_custom_button(__("Use Pairing Code"), () => pairing_code(frm), __("Session"));
		}

		frm.add_custom_button(__("Refresh Status"), () => run(frm, "refresh_status"));

		if (frm.doc.state === "Pairing") {
			frm.add_custom_button(__("Fetch QR"), () => run(frm, "fetch_qr"));
		}

		if (connected) {
			frm.add_custom_button(__("Send Test Message"), () => send_test(frm));
			frm.add_custom_button(__("Disconnect"), () => run(frm, "disconnect"), __("Session"));
		}

		if (connected || frm.doc.state === "Pairing") {
			frm.add_custom_button(
				__("Log Out"),
				() =>
					frappe.confirm(
						__("Unlink this number? You will need to scan a new QR code to reconnect."),
						() => run(frm, "logout"),
					),
				__("Session"),
			);
		}
	},

	render_pairing(frm) {
		const wrapper = frm.get_field("pairing_html")?.$wrapper;
		if (!wrapper) return;

		wrapper.empty().append(pairing_markup(frm.doc));
	},
});

function pairing_markup(doc) {
	if (doc.__islocal) {
		return block(__("Save this session, then press Connect to pair a phone."));
	}

	if (doc.state === "Connected") {
		const number = doc.phone_number ? frappe.utils.escape_html(doc.phone_number) : __("unknown number");
		return block(
			`<div style="font-size:15px;"><b>${__("Connected")}</b></div>
			 <div class="text-muted">${__("Linked to")} +${number}</div>`,
			"green",
		);
	}

	if (doc.state === "Pairing" && doc.qr_data) {
		return $(`
			<div style="text-align:center;padding:12px 0;">
				<img src="${frappe.utils.escape_html(doc.qr_data)}"
				     alt="${__("WhatsApp QR code")}"
				     style="width:260px;height:260px;image-rendering:pixelated;
				            border:1px solid var(--border-color);border-radius:8px;padding:8px;
				            background:#fff;" />
				<div class="text-muted" style="margin-top:10px;max-width:340px;
				     margin-left:auto;margin-right:auto;line-height:1.5;">
					${__("On your phone open WhatsApp, go to Linked Devices, and scan this code.")}
					<br>${__("The code refreshes automatically until it is scanned.")}
				</div>
			</div>
		`);
	}

	if (doc.state === "Pairing") {
		return block(__("Waiting for a QR code…"), "orange");
	}

	if (doc.state === "Logged Out") {
		return block(__("This number was unlinked. Press Connect to pair again."), "red");
	}

	return block(__("Not connected. Press Connect to start pairing."));
}

function block(html, indicator) {
	const dot = indicator ? `<span class="indicator ${indicator}"></span>` : "";
	return $(
		`<div style="padding:16px;border:1px dashed var(--border-color);border-radius:8px;
		     text-align:center;">${dot}${html}</div>`,
	);
}

function run(frm, method, freeze_message) {
	return frm
		.call({ doc: frm.doc, method, freeze: true, freeze_message: freeze_message || __("Working…") })
		.then((r) => {
			const result = r.message || {};
			if (result.qr_error) {
				frappe.msgprint({
					title: __("Could not fetch the QR code"),
					message: frappe.utils.escape_html(result.qr_error),
					indicator: "orange",
				});
			}
			return frm.reload_doc().then(() => r);
		});
}

function send_test(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Send Test Message"),
		fields: [
			{
				fieldname: "to",
				fieldtype: "Data",
				label: __("To"),
				reqd: 1,
				description: __("Number with country code, e.g. 254712345678"),
			},
			{
				fieldname: "message",
				fieldtype: "Small Text",
				label: __("Message"),
				reqd: 1,
				default: __("Test message from AgentX."),
			},
		],
		primary_action_label: __("Send"),
		primary_action(values) {
			frm.call({
				doc: frm.doc,
				method: "send_test",
				args: { to: values.to, message: values.message },
				freeze: true,
				freeze_message: __("Sending…"),
			}).then(() => {
				dialog.hide();
				frappe.show_alert({ message: __("Sent"), indicator: "green" });
			});
		},
	});
	dialog.show();
}

function pairing_code(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Link with a Pairing Code"),
		fields: [
			{
				fieldname: "phone",
				fieldtype: "Data",
				label: __("Phone Number"),
				reqd: 1,
				description: __("The number being linked, with country code, e.g. 254712345678"),
			},
		],
		primary_action_label: __("Get Code"),
		primary_action(values) {
			frm.call({
				doc: frm.doc,
				method: "get_pairing_code",
				args: { phone: values.phone },
				freeze: true,
				freeze_message: __("Asking for a code…"),
			}).then((r) => {
				const result = r.message || {};
				dialog.hide();

				if (!result.supported) {
					frappe.msgprint({
						title: __("Not available"),
						message: __("This provider does not offer pairing codes. Scan the QR instead."),
						indicator: "orange",
					});
					return;
				}

				frappe.msgprint({
					title: __("Pairing Code"),
					message: `
						<div style="text-align:center;padding:8px 0;">
							<div style="font-size:28px;font-weight:600;letter-spacing:3px;font-family:monospace;">
								${frappe.utils.escape_html(result.pairing_code)}
							</div>
							<div class="text-muted" style="margin-top:10px;line-height:1.5;">
								${__("On the phone open WhatsApp, go to Linked Devices, tap Link with phone number instead, and type this code.")}
							</div>
						</div>`,
					indicator: "blue",
				});

				frm.reload_doc();
			});
		},
	});
	dialog.show();
}
