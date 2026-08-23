#!/usr/bin/env python3
"""Tests for the parts of AgentX that do not need a database.

Run with:  python3 tests/test_logic.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stub_frappe import Row, install

install()

from agent_x.agent import policy, provider, registry, runtime  # noqa: E402
from agent_x.core import phone  # noqa: E402


class TestPhone(unittest.TestCase):
	def test_digits_only_strips_punctuation(self):
		self.assertEqual(phone.digits_only("+254 (712) 345-678"), "254712345678")

	def test_local_number_gains_country_code(self):
		self.assertEqual(phone.normalise("0712345678", "254"), "254712345678")

	def test_international_number_is_left_alone(self):
		self.assertEqual(phone.normalise("254712345678", "254"), "254712345678")

	def test_double_zero_prefix_is_stripped(self):
		self.assertEqual(phone.normalise("00254712345678", "254"), "254712345678")

	def test_bare_subscriber_number_gains_code(self):
		self.assertEqual(phone.normalise("712345678", "254"), "254712345678")

	def test_without_a_country_code_nothing_is_invented(self):
		self.assertEqual(phone.normalise("0712345678", None), "0712345678")

	def test_same_number_matches_across_formats(self):
		self.assertTrue(phone.same_number("0712345678", "254712345678"))

	def test_same_number_rejects_short_tails(self):
		# A short entry must not match every number ending in those digits.
		self.assertFalse(phone.same_number("123", "254712345123"))

	def test_same_number_rejects_different_people(self):
		self.assertFalse(phone.same_number("254712345678", "254799999999"))

	def test_jid_round_trip(self):
		self.assertEqual(phone.to_jid("254712345678"), "254712345678@s.whatsapp.net")
		self.assertEqual(phone.from_jid("254712345678:12@s.whatsapp.net"), "254712345678")

	def test_group_jid(self):
		self.assertEqual(phone.to_jid("12345", True), "12345@g.us")


class TestProviderTurns(unittest.TestCase):
	"""Several tools called at once must reach every provider in the shape it wants."""

	def setUp(self):
		self.turns = [
			{"role": "user", "text": "how many leads do we have?"},
			{
				"role": "assistant",
				"text": "",
				"tool_calls": [
					{"id": "a", "name": "list_documents", "args": {"doctype": "Lead"}},
					{"id": "b", "name": "count_documents", "args": {"doctype": "Lead"}},
				],
			},
			{"role": "tool", "id": "a", "name": "list_documents", "result": {"count": 2}},
			{"role": "tool", "id": "b", "name": "count_documents", "result": {"count": 7}},
		]
		self.grouped = provider.group_turns(self.turns)

	def test_consecutive_tool_results_are_bundled(self):
		self.assertEqual(len(self.grouped), 3)
		self.assertEqual(self.grouped[2]["role"], "tool_group")
		self.assertEqual(len(self.grouped[2]["items"]), 2)

	def test_gemini_alternates_roles(self):
		roles = [provider.gemini_turn(t)["role"] for t in self.grouped]
		self.assertEqual(roles, ["user", "model", "user"])

	def test_gemini_sends_every_response_in_one_turn(self):
		last = provider.gemini_turn(self.grouped[2])
		self.assertEqual(len(last["parts"]), 2)
		self.assertTrue(all("functionResponse" in p for p in last["parts"]))

	def test_anthropic_bundles_tool_results(self):
		last = provider.anthropic_turn(self.grouped[2])
		self.assertEqual(last["role"], "user")
		self.assertEqual([c["tool_use_id"] for c in last["content"]], ["a", "b"])

	def test_openai_keeps_one_message_per_result(self):
		messages = []
		for turn in self.grouped:
			messages.extend(provider.openai_turn(turn))
		self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool", "tool"])
		self.assertEqual([m["tool_call_id"] for m in messages if m["role"] == "tool"], ["a", "b"])

	def test_schema_cleaning_drops_keys_gemini_rejects(self):
		dirty = {
			"type": "object",
			"additionalProperties": False,
			"properties": {"doctype": {"type": "string", "enum": ["Lead"], "x-custom": 1}},
			"required": ["doctype"],
		}
		clean = provider.clean_schema(dirty)
		self.assertNotIn("additionalProperties", clean)
		self.assertNotIn("x-custom", clean["properties"]["doctype"])
		self.assertEqual(clean["properties"]["doctype"]["enum"], ["Lead"])
		self.assertEqual(clean["required"], ["doctype"])

	def test_function_response_is_always_an_object(self):
		self.assertEqual(provider.wrap_result({"a": 1}), {"a": 1})
		self.assertEqual(provider.wrap_result("plain"), {"result": "plain"})


class TestConfirmation(unittest.TestCase):
	"""A change only happens on a clear yes."""

	def test_plain_agreement(self):
		for text in ("yes", "YES", "Yes!", "y", "ok", "sure", "confirm", "sawa", "ndio"):
			self.assertEqual(runtime.normalise_answer(text), "yes", text)

	def test_short_agreement_phrase(self):
		self.assertEqual(runtime.normalise_answer("yes please"), "yes")
		self.assertEqual(runtime.normalise_answer("go ahead"), "yes")

	def test_plain_refusal(self):
		for text in ("no", "No.", "nope", "cancel", "stop", "hapana"):
			self.assertEqual(runtime.normalise_answer(text), "no", text)

	def test_empty_is_not_consent(self):
		self.assertEqual(runtime.normalise_answer(""), "unclear")

	def test_a_question_is_not_consent(self):
		self.assertEqual(runtime.normalise_answer("what does that mean?"), "unclear")

	def test_conditional_agreement_is_not_consent(self):
		# The dangerous case: it starts with "yes" but changes the request.
		self.assertEqual(
			runtime.normalise_answer("yes but only if the total is under 5000 and change the address"),
			"unclear",
		)

	def test_a_sentence_containing_no_is_not_a_refusal(self):
		self.assertEqual(
			runtime.normalise_answer("well I said no earlier but let us go ahead maybe"), "unclear"
		)

	def test_reply_is_clamped_to_the_limit(self):
		clamped = runtime.clamp("word " * 50, 20)
		self.assertLessEqual(len(clamped), 23)
		self.assertTrue(clamped.endswith("..."))

	def test_short_reply_is_untouched(self):
		self.assertEqual(runtime.clamp("hello", 100), "hello")


class TestToolArguments(unittest.TestCase):
	def test_json_string_is_parsed(self):
		out = registry.decode_json_arguments({"filters": '{"status":"Draft"}'})
		self.assertEqual(out["filters"], {"status": "Draft"})

	def test_a_real_dict_passes_through(self):
		self.assertEqual(registry.decode_json_arguments({"filters": {"a": 1}})["filters"], {"a": 1})

	def test_blank_is_dropped(self):
		self.assertNotIn("filters", registry.decode_json_arguments({"filters": "   "}))

	def test_malformed_json_is_reported(self):
		with self.assertRaises(ValueError) as caught:
			registry.decode_json_arguments({"values": "not json"})
		self.assertIn("valid JSON", str(caught.exception))

	def test_an_array_is_rejected(self):
		with self.assertRaises(ValueError):
			registry.decode_json_arguments({"values": "[1,2]"})

	def test_unknown_tool_returns_an_error_not_an_exception(self):
		self.assertIn("no tool", registry.call("nonexistent", {}, None)["error"])


class FakeSettings:
	def __init__(
		self,
		policies,
		automation_enabled=True,
		confirm_before_write=0,
		allow_document_pdfs=0,
		enable_catalogue=0,
	):
		self.doctype_policies = policies
		self.automation_enabled = automation_enabled
		self.confirm_before_write = confirm_before_write
		self.allow_document_pdfs = allow_document_pdfs
		self.enable_catalogue = enable_catalogue

	def policy_for(self, doctype):
		return next((r for r in self.doctype_policies if r.document_type == doctype), None)


class TestRegistrySchemas(unittest.TestCase):
	"""The model is only told about tools the policy actually allows."""

	def test_no_automation_means_no_tools(self):
		self.assertEqual(registry.build_schemas(FakeSettings([], automation_enabled=False)), [])

	def test_read_only_policy_advertises_no_write_tools(self):
		settings = FakeSettings([Row(document_type="Lead", can_read=1)])
		names = {t["name"] for t in registry.build_schemas(settings)}
		self.assertEqual(names, {"list_documents", "get_document", "count_documents", "describe_doctype"})

	def test_write_tools_are_scoped_to_their_own_doctypes(self):
		settings = FakeSettings(
			[
				Row(document_type="Lead", can_read=1, can_create=1),
				Row(document_type="Sales Invoice", can_read=1, can_submit=1),
			]
		)
		schemas = {t["name"]: t for t in registry.build_schemas(settings)}

		self.assertEqual(schemas["create_document"]["parameters"]["properties"]["doctype"]["enum"], ["Lead"])
		self.assertEqual(
			schemas["submit_document"]["parameters"]["properties"]["doctype"]["enum"], ["Sales Invoice"]
		)
		# Reading spans both.
		self.assertEqual(
			schemas["list_documents"]["parameters"]["properties"]["doctype"]["enum"],
			["Lead", "Sales Invoice"],
		)

	def test_free_form_arguments_are_declared_as_strings(self):
		# Gemini rejects an object schema with no declared properties.
		settings = FakeSettings([Row(document_type="Lead", can_read=1, can_create=1)])
		schemas = {t["name"]: t for t in registry.build_schemas(settings)}
		self.assertEqual(schemas["create_document"]["parameters"]["properties"]["values"]["type"], "string")
		self.assertEqual(schemas["list_documents"]["parameters"]["properties"]["filters"]["type"], "string")

	def test_every_schema_survives_gemini_cleaning(self):
		settings = FakeSettings(
			[Row(document_type="Lead", can_read=1, can_create=1, can_write=1, can_delete=1)]
		)
		for tool in registry.build_schemas(settings):
			cleaned = provider.clean_schema(tool["parameters"])
			self.assertIn("properties", cleaned, tool["name"])
			self.assertEqual(cleaned.get("required"), tool["parameters"].get("required"), tool["name"])


class TestPolicyGate(unittest.TestCase):
	def setUp(self):
		self.lead = Row(
			document_type="Lead", can_read=1, can_create=1, can_write=1, requires_approval=0, max_per_day=0
		)
		self.settings = FakeSettings([self.lead])

		# Isolate the policy table from Frappe's own permission check.
		self._perm = policy.has_permission_as
		self._limit = policy.daily_limit_reached
		policy.has_permission_as = lambda user, dt, perm, name: True
		policy.daily_limit_reached = lambda p, dt: ""

	def tearDown(self):
		policy.has_permission_as = self._perm
		policy.daily_limit_reached = self._limit

	def test_ticked_operations_are_allowed(self):
		self.assertTrue(policy.check(self.settings, "Lead", "read", "u@x.com"))
		self.assertTrue(policy.check(self.settings, "Lead", "create", "u@x.com"))

	def test_unticked_operations_are_refused(self):
		self.assertFalse(policy.check(self.settings, "Lead", "submit", "u@x.com"))
		self.assertFalse(policy.check(self.settings, "Lead", "delete", "u@x.com"))

	def test_a_doctype_outside_the_policy_is_refused(self):
		decision = policy.check(self.settings, "Sales Invoice", "read", "u@x.com")
		self.assertFalse(decision)
		self.assertIn("not in the list", decision.reason)

	def test_access_granting_doctypes_can_never_be_automated(self):
		for doctype in ("User", "Role", "Server Script", "AgentX Settings", "Custom DocPerm"):
			listed = FakeSettings([Row(document_type=doctype, can_read=1, can_write=1)])
			self.assertFalse(policy.check(listed, doctype, "write", "u@x.com"), doctype)

	def test_an_unmapped_number_can_do_nothing(self):
		decision = policy.check(self.settings, "Lead", "read", None)
		self.assertFalse(decision)
		self.assertIn("not linked to a user", decision.reason)

	def test_automation_switch_overrides_the_policy(self):
		off = FakeSettings([self.lead], automation_enabled=False)
		self.assertFalse(policy.check(off, "Lead", "read", "u@x.com"))

	def test_frappe_permission_denial_wins(self):
		policy.has_permission_as = lambda user, dt, perm, name: False
		decision = policy.check(self.settings, "Lead", "create", "u@x.com")
		self.assertFalse(decision)
		self.assertIn("does not have permission", decision.reason)

	def test_unknown_operations_are_refused(self):
		self.assertFalse(policy.check(self.settings, "Lead", "frobnicate", "u@x.com"))

	def test_global_confirmation_forces_approval(self):
		settings = FakeSettings([self.lead], confirm_before_write=1)
		self.assertTrue(policy.check(settings, "Lead", "create", "u@x.com").needs_approval)

	def test_per_doctype_confirmation_forces_approval(self):
		row = Row(document_type="Lead", can_read=1, can_create=1, requires_approval=1, max_per_day=0)
		self.assertTrue(policy.check(FakeSettings([row]), "Lead", "create", "u@x.com").needs_approval)

	def test_reading_never_needs_approval(self):
		settings = FakeSettings([self.lead], confirm_before_write=1)
		self.assertFalse(policy.check(settings, "Lead", "read", "u@x.com").needs_approval)

	def test_daily_cap_blocks_writes_but_not_reads(self):
		policy.daily_limit_reached = lambda p, dt: "limit reached"
		self.assertFalse(policy.check(self.settings, "Lead", "create", "u@x.com"))
		self.assertTrue(policy.check(self.settings, "Lead", "read", "u@x.com"))

	def test_denied_decisions_raise_on_demand(self):
		with self.assertRaises(policy.PolicyError):
			policy.check(self.settings, "Sales Invoice", "read", "u@x.com").raise_if_denied()


class TestFieldAllowlist(unittest.TestCase):
	def test_listed_fields_pass(self):
		row = Row(allowed_fields="status,notes")
		self.assertEqual(policy.allowed_fields(row, "Lead", {"status": "Open"}), {"status": "Open"})

	def test_unlisted_fields_are_refused(self):
		row = Row(allowed_fields="status,notes")
		with self.assertRaises(policy.PolicyError) as caught:
			policy.allowed_fields(row, "Lead", {"status": "Open", "grand_total": 9})
		self.assertIn("grand_total", str(caught.exception))

	def test_an_empty_allowlist_narrows_nothing(self):
		self.assertEqual(policy.allowed_fields(Row(allowed_fields=""), "Lead", {"x": 1}), {"x": 1})


class TestPromptDescription(unittest.TestCase):
	def test_only_permitted_verbs_are_described(self):
		row = Row(document_type="Lead", can_read=1, can_create=1)
		text = policy.describe_for_prompt(FakeSettings([row]))
		self.assertIn("Lead", text)
		self.assertIn("create", text)
		self.assertNotIn("submit", text)

	def test_disabled_automation_says_so_plainly(self):
		text = policy.describe_for_prompt(FakeSettings([], automation_enabled=False))
		self.assertIn("cannot read or change", text)

	def test_approval_requirement_is_mentioned(self):
		row = Row(document_type="Lead", can_read=1, can_create=1, requires_approval=1)
		self.assertIn("confirm", policy.describe_for_prompt(FakeSettings([row])))




# --------------------------------------------------------------------------
# Provider-specific handling. WaClient and the self-hosted bridge send very
# different payloads, and both have to arrive at the same internal shape.
# --------------------------------------------------------------------------

from agent_x.core import payload as wa_payload  # noqa: E402
from agent_x.core.transport import base as transport_base  # noqa: E402


def envelope(text=None, media=None, remote="254712345678@s.whatsapp.net", from_me=False, msg_id="MSG1"):
	"""A Baileys message envelope, as WaClient forwards it."""
	content = {"conversation": text} if text is not None else dict(media or {})
	return {
		"key": {"remoteJid": remote, "fromMe": from_me, "id": msg_id},
		"pushName": "Jane",
		"messageTimestamp": 1700000000,
		"message": content,
	}


class TestWaClientPayload(unittest.TestCase):
	def test_plain_text_message(self):
		parsed = wa_payload.parse(
			{"instance_id": "inst1", "event": "messages.upsert", "data": {"message": envelope("hello there")}}
		)
		self.assertEqual(parsed["text"], "hello there")
		self.assertEqual(parsed["wa_id"], "254712345678")
		self.assertEqual(parsed["message_id"], "MSG1")
		self.assertEqual(parsed["push_name"], "Jane")
		self.assertEqual(parsed["instance_id"], "inst1")
		self.assertFalse(parsed["is_group"])
		self.assertFalse(parsed["from_me"])

	def test_older_body_message_nesting(self):
		# Older WaClient builds wrap the envelope in body_message.messages.
		parsed = wa_payload.parse(
			{
				"instance_id": "inst1",
				"data": {"message": {"body_message": {"messages": [envelope("nested hello")]}}},
			}
		)
		self.assertEqual(parsed["text"], "nested hello")
		self.assertEqual(parsed["wa_id"], "254712345678")

	def test_extended_text_message(self):
		parsed = wa_payload.parse(
			{"data": {"message": envelope(media={"extendedTextMessage": {"text": "a reply"}})}}
		)
		self.assertEqual(parsed["text"], "a reply")
		self.assertEqual(parsed["message_type"], "text")

	def test_image_with_caption(self):
		parsed = wa_payload.parse(
			{
				"data": {
					"message": envelope(
						media={
							"imageMessage": {
								"caption": "look at this",
								"url": "https://example.com/i.jpg",
								"fileName": "i.jpg",
								"mimetype": "image/jpeg",
								"fileLength": "2048",
							}
						}
					)
				}
			}
		)
		self.assertEqual(parsed["text"], "look at this")
		self.assertEqual(parsed["message_type"], "image")
		self.assertEqual(parsed["media"]["filename"], "i.jpg")
		self.assertEqual(parsed["media"]["size"], 2048)

	def test_group_message_is_flagged(self):
		parsed = wa_payload.parse({"data": {"message": envelope("hi", remote="12345-678@g.us")}})
		self.assertTrue(parsed["is_group"])

	def test_our_own_echo_is_flagged(self):
		parsed = wa_payload.parse({"data": {"message": envelope("sent by us", from_me=True)}})
		self.assertTrue(parsed["from_me"])

	def test_disappearing_message_is_unwrapped(self):
		inner = {"ephemeralMessage": {"message": {"conversation": "vanishing"}}}
		parsed = wa_payload.parse({"data": {"message": envelope(media=inner)}})
		self.assertEqual(parsed["text"], "vanishing")

	def test_view_once_message_is_unwrapped(self):
		inner = {"viewOnceMessageV2": {"message": {"conversation": "once only"}}}
		parsed = wa_payload.parse({"data": {"message": envelope(media=inner)}})
		self.assertEqual(parsed["text"], "once only")

	def test_button_reply_reads_as_the_choice(self):
		inner = {"buttonsResponseMessage": {"selectedDisplayText": "Yes please"}}
		parsed = wa_payload.parse({"data": {"message": envelope(media=inner)}})
		self.assertEqual(parsed["text"], "Yes please")

	def test_location_message(self):
		inner = {"locationMessage": {"degreesLatitude": -1.28, "degreesLongitude": 36.81}}
		parsed = wa_payload.parse({"data": {"message": envelope(media=inner)}})
		self.assertEqual(parsed["message_type"], "location")
		self.assertIn("-1.28", parsed["text"])

	def test_empty_payload_yields_nothing(self):
		self.assertIsNone(wa_payload.parse({}))

	def test_millisecond_timestamps_are_reduced_to_seconds(self):
		env = envelope("hi")
		env["messageTimestamp"] = 1700000000000
		parsed = wa_payload.parse({"data": {"message": env}})
		self.assertEqual(parsed["timestamp"], 1700000000)

	def test_ack_events_are_recognised(self):
		parsed = {"event": "messages.ack", "message_id": "MSG1"}
		self.assertTrue(wa_payload.is_status_event(parsed))
		self.assertEqual(wa_payload.extract_ack({"data": {"status": "3"}}, parsed), "Delivered")
		self.assertEqual(wa_payload.extract_ack({"data": {"ack": "read"}}, parsed), "Read")

	def test_a_normal_message_is_not_an_ack(self):
		self.assertFalse(wa_payload.is_status_event({"event": "messages.upsert"}))


class TestQrConversion(unittest.TestCase):
	def test_a_data_url_passes_straight_through(self):
		url = "data:image/png;base64,iVBORw0KGgoAAAA"
		self.assertEqual(transport_base.qr_to_data_url(url), url)

	def test_bare_base64_png_gains_a_prefix(self):
		out = transport_base.qr_to_data_url("iVBORw0KGgoAAAANSUhEUg")
		self.assertEqual(out, "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg")

	def test_bare_base64_jpeg_gains_a_prefix(self):
		out = transport_base.qr_to_data_url("/9j/4AAQSkZJRg")
		self.assertTrue(out.startswith("data:image/jpeg;base64,"))

	def test_nothing_in_nothing_out(self):
		self.assertIsNone(transport_base.qr_to_data_url(None))
		self.assertIsNone(transport_base.qr_to_data_url(""))

	def test_a_raw_pairing_string_is_drawn(self):
		try:
			import qrcode  # noqa: F401
		except ImportError:
			self.skipTest("the qrcode package is not installed in this interpreter")

		out = transport_base.qr_to_data_url("2@abc123,def456,ghi789=")
		self.assertTrue(out.startswith("data:image/png;base64,"))
		self.assertGreater(len(out), 200)


class TestStateNormalisation(unittest.TestCase):
	def test_words_meaning_connected(self):
		for word in ("connected", "open", "authenticated", "ONLINE", "ready"):
			self.assertEqual(transport_base.normalise_state(word), "connected", word)

	def test_words_meaning_pairing(self):
		for word in ("pairing", "connecting", "qr", "scan_qr", "got_qr"):
			self.assertEqual(transport_base.normalise_state(word), "pairing", word)

	def test_words_meaning_logged_out(self):
		for word in ("logged_out", "loggedOut", "unpaired"):
			self.assertEqual(transport_base.normalise_state(word), "logged_out", word)

	def test_anything_unrecognised_is_treated_as_disconnected(self):
		# Safer to under-report the connection than to claim one that is not there.
		for word in ("", None, "banana", "close"):
			self.assertEqual(transport_base.normalise_state(word), "disconnected", repr(word))


class TestWaClientResponses(unittest.TestCase):
	"""Shapes taken from https://waclient.com/docs/whatsapp-web-api."""

	def test_qr_comes_back_under_base64_as_documented(self):
		from agent_x.core.transport.waclient import extract_qr

		documented = {
			"status": "success",
			"message": "Success",
			"base64": "data:image/png;base64,iVBORw0KGgo",
		}
		self.assertEqual(extract_qr(documented), "data:image/png;base64,iVBORw0KGgo")

	def test_qr_is_still_found_under_older_names(self):
		from agent_x.core.transport.waclient import extract_qr

		prefixed = "data:image/png;base64,iVBORw0KGgoAAA"
		for body in (
			{"data": {"qrcode": prefixed}},
			{"data": {"qr_code": prefixed}},
			{"qr": prefixed},
			{"data": {"base64": prefixed}},
		):
			self.assertEqual(extract_qr(body), prefixed, body)

	def test_no_qr_returns_nothing(self):
		from agent_x.core.transport.waclient import extract_qr

		self.assertIsNone(extract_qr({"status": "error", "message": "Instance not found"}))

	def test_message_id_comes_from_message_payload(self):
		from agent_x.core.transport.waclient import extract_message_id

		# This is the documented send response. Reading it wrong means every
		# outgoing message is logged without an id and no receipt ever matches.
		documented = {
			"status": "success",
			"message": "Success",
			"message_payload": {
				"key": {"remoteJid": "201234567890@s.whatsapp.net", "fromMe": True, "id": "ABC123"},
				"status": "PENDING",
			},
		}
		self.assertEqual(extract_message_id(documented), "ABC123")

	def test_message_id_falls_back_to_older_keys(self):
		from agent_x.core.transport.waclient import extract_message_id

		self.assertEqual(extract_message_id({"data": {"key": {"id": "OLD1"}}}), "OLD1")
		self.assertEqual(extract_message_id({"message": {"key": {"id": "OLD2"}}}), "OLD2")

	def test_missing_message_id_is_not_invented(self):
		from agent_x.core.transport.waclient import extract_message_id

		self.assertIsNone(extract_message_id({"status": "success", "message": "Success"}))

	def test_a_bare_number_and_a_jid_are_addressed_differently(self):
		from agent_x.core.transport.waclient import WaClientTransport

		# Groups and channels must go out as chat_id, plain numbers as number.
		self.assertEqual(WaClientTransport.target("254712345678"), {"number": "254712345678"})
		self.assertEqual(WaClientTransport.target("+254 712 345 678"), {"number": "254712345678"})
		self.assertEqual(
			WaClientTransport.target("120363000000000000@g.us"),
			{"chat_id": "120363000000000000@g.us"},
		)
		self.assertEqual(
			WaClientTransport.target("120363000000000000@newsletter"),
			{"chat_id": "120363000000000000@newsletter"},
		)


class TestDocumentedConnectionStates(unittest.TestCase):
	"""WaClient documents: pending, linking, connecting, connected, disconnected, logged_out."""

	def test_every_documented_state_maps_somewhere_sensible(self):
		expected = {
			"pending": "pairing",
			"linking": "pairing",
			"connecting": "pairing",
			"connected": "connected",
			"disconnected": "disconnected",
			"logged_out": "logged_out",
		}
		for documented, ours in expected.items():
			self.assertEqual(transport_base.normalise_state(documented), ours, documented)

	def test_a_state_we_have_never_seen_is_treated_as_disconnected(self):
		# Under-reporting the connection is safer than claiming one that is absent.
		self.assertEqual(transport_base.normalise_state("teleporting"), "disconnected")


class TestDocumentPdfs(unittest.TestCase):
	def test_pdf_tool_is_hidden_when_switched_off(self):
		settings = FakeSettings([Row(document_type="Sales Order", can_read=1)], allow_document_pdfs=0)
		names = {t["name"] for t in registry.build_schemas(settings)}
		self.assertNotIn("send_document", names)

	def test_pdf_tool_appears_when_allowed(self):
		settings = FakeSettings([Row(document_type="Sales Order", can_read=1)], allow_document_pdfs=1)
		names = {t["name"] for t in registry.build_schemas(settings)}
		self.assertIn("send_document", names)

	def test_pdf_tool_is_scoped_to_readable_doctypes(self):
		settings = FakeSettings(
			[Row(document_type="Sales Order", can_read=1), Row(document_type="Lead", can_read=1)],
			allow_document_pdfs=1,
		)
		schemas = {t["name"]: t for t in registry.build_schemas(settings)}
		self.assertEqual(
			schemas["send_document"]["parameters"]["properties"]["doctype"]["enum"],
			["Lead", "Sales Order"],
		)

	def test_sending_a_pdf_needs_only_read_permission(self):
		# It discloses what the document says, it does not change anything.
		self.assertEqual(registry.TOOL_OPERATIONS["send_document"], "read")


class TestPdfFilenames(unittest.TestCase):
	def setUp(self):
		from agent_x.core import printing

		self.printing = printing

	def test_public_filename_carries_an_unguessable_suffix(self):
		# A public PDF is readable by anyone with the URL, so the name must not
		# be derivable from the document number.
		name = self.printing.build_filename("Sales Order", "SAL-ORD-2026-00001")
		self.assertTrue(name.startswith("agentx-"))
		self.assertTrue(name.endswith(".pdf"))
		self.assertIn("a1b2c3d4e5f6", name)

	def test_private_filename_stays_readable(self):
		name = self.printing.build_filename("Sales Order", "SAL-ORD-2026-00001", unique=False)
		self.assertEqual(name, "Sales-Order-SAL-ORD-2026-00001.pdf")

	def test_awkward_characters_are_replaced(self):
		name = self.printing.build_filename("Sales Order", "SO/2026/001 #2", unique=False)
		self.assertNotIn("/", name)
		self.assertNotIn("#", name)
		self.assertNotIn(" ", name)
		self.assertTrue(name.endswith(".pdf"))

	def test_the_cleanup_prefix_matches_what_is_generated(self):
		# If these drift, generated PDFs stay public forever.
		generated = self.printing.build_filename("Lead", "CRM-LEAD-0001")
		self.assertTrue(generated.startswith(self.printing.PREFIX))


class TestTransportMediaCapability(unittest.TestCase):
	def test_hosted_provider_needs_a_public_url(self):
		from agent_x.core.transport.waclient import WaClientTransport

		self.assertTrue(WaClientTransport.needs_public_media)

	def test_self_hosted_bridge_takes_raw_bytes(self):
		# Which means its PDFs never have to be exposed publicly.
		from agent_x.core.transport.bridge import BridgeTransport

		self.assertFalse(BridgeTransport.needs_public_media)


# --------------------------------------------------------------------------
# Taking an order. The model writes the conversation, but never the figures
# someone is agreeing to, so the rendering below is what actually protects the
# customer from a mistaken order.
# --------------------------------------------------------------------------

from agent_x.agent import summary as order_summary  # noqa: E402
from agent_x.agent.tools import catalogue  # noqa: E402


def order(lines, **kw):
	payload = {"customer": "Acme Ltd", "currency": "KES", "items": lines}
	payload.update(kw)
	return payload


def line(n, qty=1, rate=100):
	return {"item_code": f"ITEM-{n:04d}", "item_name": f"Widget {n}", "qty": qty, "rate": rate}


class TestOrderSummary(unittest.TestCase):
	def test_the_customer_is_named(self):
		text = order_summary.describe_payload("Sales Order", order([line(1)]))
		self.assertIn("Acme Ltd", text)

	def test_every_line_is_listed_with_its_maths(self):
		text = order_summary.describe_payload("Sales Order", order([line(1, qty=3, rate=50)]))
		self.assertIn("ITEM-0001", text)
		self.assertIn("x3", text)
		self.assertIn("150.00", text)

	def test_the_total_is_computed_not_taken_on_trust(self):
		lines = [line(1, qty=2, rate=100), line(2, qty=3, rate=50)]
		text = order_summary.describe_payload("Sales Order", order(lines))
		self.assertIn("350.00", text)

	def test_a_twenty_line_order_lists_every_line(self):
		lines = [line(n, qty=n, rate=100) for n in range(1, 21)]
		text = order_summary.describe_payload("Sales Order", order(lines))
		for n in (1, 10, 20):
			self.assertIn(f"ITEM-{n:04d}", text, f"line {n} missing")
		self.assertIn("20 lines", text)

	def test_a_twenty_line_order_fits_in_one_whatsapp_message(self):
		lines = [line(n, qty=n, rate=1000) for n in range(1, 21)]
		text = order_summary.describe_payload("Sales Order", order(lines))
		self.assertLess(len(text), order_summary.CONFIRMATION_LIMIT)

	def test_a_very_long_order_is_summarised_rather_than_truncated(self):
		lines = [line(n, qty=1, rate=10) for n in range(1, 61)]
		text = order_summary.describe_payload("Sales Order", order(lines))
		self.assertIn("more lines", text)
		self.assertIn("60 lines", text)
		# The tail still has to be counted, or the total would understate the order.
		self.assertIn("600.00", text)

	def test_a_line_without_a_price_is_not_given_one(self):
		text = order_summary.describe_payload(
			"Sales Order", order([{"item_code": "ITEM-X", "qty": 2}])
		)
		self.assertIn("ITEM-X", text)
		self.assertIn("x2", text)
		self.assertNotIn("@", text)

	def test_a_document_with_no_lines_still_describes_itself(self):
		text = order_summary.describe_payload("Lead", {"lead_name": "Jane", "status": "Open"})
		self.assertIn("Lead", text)
		self.assertIn("Jane", text)


class FakeAction:
	def __init__(self, action, doctype, payload, name=None):
		self.action = action
		self.document_type = doctype
		self.document_name = name
		self._payload = payload

	def get_payload(self):
		return self._payload


class TestConfirmationMessage(unittest.TestCase):
	def test_creating_asks_for_a_yes(self):
		text = order_summary.for_action(
			FakeAction("create", "Sales Order", order([line(1, qty=2, rate=500)]))
		)
		self.assertIn("about to create", text)
		self.assertIn("YES", text)
		self.assertIn("ITEM-0001", text)
		self.assertIn("1,000.00", text)

	def test_submitting_is_worded_as_final(self):
		text = order_summary.for_action(FakeAction("submit", "Sales Order", {}, "SAL-ORD-0001"))
		self.assertIn("submit", text.lower())
		self.assertIn("YES", text)

	def test_deleting_says_it_is_permanent(self):
		text = order_summary.for_action(FakeAction("delete", "Sales Order", {}, "SAL-ORD-0001"))
		self.assertIn("permanently", text.lower())

	def test_every_action_offers_a_way_out(self):
		for verb in ("create", "update", "submit", "cancel", "delete"):
			text = order_summary.for_action(FakeAction(verb, "Sales Order", order([line(1)]), "SO-1"))
			self.assertIn("NO", text, verb)


class TestCatalogueHelpers(unittest.TestCase):
	def test_html_descriptions_are_flattened_for_chat(self):
		out = catalogue.short("<p>A <b>strong</b> widget</p>")
		self.assertEqual(out, "A strong widget")

	def test_long_descriptions_are_cut_at_a_word(self):
		out = catalogue.short("word " * 100, limit=40)
		self.assertLessEqual(len(out), 44)
		self.assertTrue(out.endswith("..."))

	def test_an_empty_description_is_not_a_crash(self):
		self.assertEqual(catalogue.short(None), "")

	def test_the_result_cap_is_enforced(self):
		# A chat cannot show hundreds of items, and the model should not try.
		self.assertEqual(catalogue.MAX_ITEMS, 25)


class TestCatalogueTools(unittest.TestCase):
	def test_catalogue_is_hidden_without_an_item_policy(self):
		# Browsing reads Items, so it must obey the same gate as anything else.
		settings = FakeSettings([Row(document_type="Lead", can_read=1)], enable_catalogue=1)
		names = {t["name"] for t in registry.build_schemas(settings)}
		self.assertNotIn("find_items", names)

	def test_catalogue_is_hidden_when_switched_off(self):
		settings = FakeSettings([Row(document_type="Item", can_read=1)], enable_catalogue=0)
		names = {t["name"] for t in registry.build_schemas(settings)}
		self.assertNotIn("find_items", names)

	def test_catalogue_appears_with_item_read_and_the_switch_on(self):
		settings = FakeSettings([Row(document_type="Item", can_read=1)], enable_catalogue=1)
		catalogue.available = lambda: True
		names = {t["name"] for t in registry.build_schemas(settings)}
		self.assertEqual(
			{"find_items", "get_item", "list_item_groups"} & names,
			{"find_items", "get_item", "list_item_groups"},
		)

	def test_browsing_only_ever_needs_read(self):
		for tool in ("find_items", "get_item", "list_item_groups"):
			self.assertEqual(registry.TOOL_OPERATIONS[tool], "read", tool)


# --------------------------------------------------------------------------
# Retrieval. The point of it is to spend fewer tokens per message, so the
# chunking and the trivial-query gate are what actually decide whether it pays.
# --------------------------------------------------------------------------

from agent_x.agent import knowledge as kb  # noqa: E402


class TestVectorPacking(unittest.TestCase):
	def test_round_trip_keeps_enough_precision(self):
		vector = [0.1, -0.5, 0.98765, 0.0, 1.0]
		restored = kb.unpack(kb.pack(vector))
		for original, back in zip(vector, restored):
			self.assertAlmostEqual(original, back, places=6)

	def test_packing_is_much_smaller_than_json(self):
		import json as jsonlib
		import random

		random.seed(1)
		vector = [random.uniform(-1, 1) for _ in range(768)]
		self.assertLess(len(kb.pack(vector)), len(jsonlib.dumps(vector)) / 2)

	def test_dimensions_survive(self):
		self.assertEqual(len(kb.unpack(kb.pack([0.5] * 768))), 768)


class TestChunking(unittest.TestCase):
	def test_paragraphs_are_kept_together_where_they_fit(self):
		text = "First rule.\n\nSecond rule.\n\nThird rule."
		self.assertEqual(len(kb.chunk_text(text, size=1000, overlap=0)), 1)

	def test_chunks_respect_the_size_budget(self):
		text = "\n\n".join(f"Paragraph {i}. " + ("word " * 30) for i in range(1, 8))
		for chunk in kb.chunk_text(text, size=400, overlap=50):
			self.assertLessEqual(len(chunk), 460)

	def test_one_enormous_paragraph_is_still_broken_up(self):
		# Otherwise a single wall of text would be one unretrievable chunk.
		chunks = kb.chunk_text("x" * 3000, size=500, overlap=0)
		self.assertGreater(len(chunks), 1)
		self.assertLessEqual(max(len(c) for c in chunks), 500)

	def test_overlap_carries_context_across_a_boundary(self):
		chunks = kb.chunk_text("First rule about refunds.\n\nSecond rule about delivery.", size=30, overlap=15)
		self.assertGreater(len(chunks), 1)
		# The second chunk should still mention what came before it.
		self.assertIn("refunds", chunks[1])

	def test_nothing_in_nothing_out(self):
		self.assertEqual(kb.chunk_text(""), [])
		self.assertEqual(kb.chunk_text(None), [])

	def test_no_chunk_is_empty(self):
		text = "\n\n\n".join(["Real content here.", "", "   ", "More content."])
		self.assertTrue(all(c.strip() for c in kb.chunk_text(text, size=100)))


class TestTrivialQueryGate(unittest.TestCase):
	"""Every retrieval costs an embedding call, so greetings must not trigger one."""

	def test_greetings_are_skipped(self):
		for text in ("hi", "Hello", "hey", "good morning", "asante", "sawa"):
			self.assertTrue(kb.is_trivial(text), text)

	def test_confirmations_are_skipped(self):
		for text in ("yes", "no", "ok", "thanks", "ndio"):
			self.assertTrue(kb.is_trivial(text), text)

	def test_punctuation_does_not_defeat_the_gate(self):
		self.assertTrue(kb.is_trivial("Hello!"))
		self.assertTrue(kb.is_trivial("ok."))

	def test_a_real_question_is_looked_up(self):
		for text in ("what is your refund policy?", "do you deliver to Mombasa", "how much is delivery"):
			self.assertFalse(kb.is_trivial(text), text)

	def test_retrieval_is_skipped_entirely_when_switched_off(self):
		class Off:
			knowledge_enabled = 0

		self.assertEqual(kb.context_for("what is your refund policy?", Off()), ("", []))


class TestTokenEstimate(unittest.TestCase):
	def test_estimate_is_in_the_right_ballpark(self):
		self.assertEqual(kb.estimate_tokens("x" * 1200), 300)

	def test_empty_text_still_counts_as_something(self):
		self.assertEqual(kb.estimate_tokens(""), 1)


if __name__ == "__main__":
	unittest.main(verbosity=2)
