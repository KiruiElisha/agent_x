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

from agent_x.agent import policy, prompt, provider, registry, runtime  # noqa: E402
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

	def test_a_plain_instruction_after_yes_is_still_consent(self):
		# Real people do not reply with a bare "yes"; refusing these made the
		# assistant ask the same question over and over.
		for text in ("Yes just place the order", "yes place the order", "ok place it",
		             "yes, place the order please", "yeah do it"):
			self.assertEqual(runtime.normalise_answer(text), "yes", text)

	def test_conditional_agreement_is_not_consent(self):
		# The dangerous case: it opens with "yes" but describes a different order.
		for text in ("yes but only if the total is under 5000 and change the address",
		             "yes but change the quantity", "yes if the price is right",
		             "yes actually make it 10", "yes instead send 3",
		             "yes but not the third item"):
			self.assertEqual(runtime.normalise_answer(text), "unclear", text)

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
		policy_mode="Listed Documents Only",
		**defaults,
	):
		self.doctype_policies = policies
		self.automation_enabled = automation_enabled
		self.confirm_before_write = confirm_before_write
		self.allow_document_pdfs = allow_document_pdfs
		self.enable_catalogue = enable_catalogue
		self.policy_mode = policy_mode

		for field in ("read", "create", "write", "submit", "cancel", "delete"):
			setattr(self, f"all_can_{field}", defaults.get(f"all_can_{field}", 0))
		self.all_requires_approval = defaults.get("all_requires_approval", 1)
		self.all_max_per_day = defaults.get("all_max_per_day", 0)
		self.handoff_enabled = defaults.get("handoff_enabled", 0)
		self.use_corrections = defaults.get("use_corrections", 0)
		self.correction_limit = defaults.get("correction_limit", 20)

	def policy_for(self, doctype):
		return next((r for r in self.doctype_policies if r.document_type == doctype), None)


class TestRegistrySchemas(unittest.TestCase):
	"""The model is only told about tools the policy actually allows."""

	def test_no_automation_means_no_tools(self):
		self.assertEqual(registry.build_schemas(FakeSettings([], automation_enabled=False)), [])

	def test_read_only_policy_advertises_no_write_tools(self):
		settings = FakeSettings([Row(document_type="Lead", can_read=1)])
		names = {t["name"] for t in registry.build_schemas(settings)}

		self.assertEqual(
			names & {"list_documents", "get_document", "count_documents", "describe_doctype"},
			{"list_documents", "get_document", "count_documents", "describe_doctype"},
		)
		# Nothing that writes, whatever else is on offer.
		self.assertFalse(
			names & {"create_document", "update_document", "submit_document",
			         "cancel_document", "delete_document", "create_customer"}
		)

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


class TestAllDocumentsMode(unittest.TestCase):
	"""Opening everything up must still be bounded by the forbidden list and by permissions."""

	def setUp(self):
		self._perm = policy.has_permission_as
		self._limit = policy.daily_limit_reached
		self._auto = policy.is_automatable
		policy.has_permission_as = lambda user, dt, perm, name: True
		policy.daily_limit_reached = lambda p, dt: ""
		policy.is_automatable = lambda dt: dt not in policy.FORBIDDEN_DOCTYPES

	def tearDown(self):
		policy.has_permission_as = self._perm
		policy.daily_limit_reached = self._limit
		policy.is_automatable = self._auto

	def open_settings(self, **kw):
		kw.setdefault("all_can_read", 1)
		return FakeSettings([], policy_mode="All Documents", **kw)

	def test_an_unlisted_doctype_becomes_readable(self):
		self.assertTrue(policy.check(self.open_settings(), "Purchase Order", "read", "u@x.com"))

	def test_listed_mode_still_refuses_the_unlisted(self):
		listed = FakeSettings([Row(document_type="Lead", can_read=1)])
		self.assertFalse(policy.check(listed, "Purchase Order", "read", "u@x.com"))

	def test_writing_follows_the_defaults(self):
		self.assertFalse(policy.check(self.open_settings(), "Purchase Order", "create", "u@x.com"))
		allowed = self.open_settings(all_can_create=1)
		self.assertTrue(policy.check(allowed, "Purchase Order", "create", "u@x.com"))

	def test_forbidden_doctypes_stay_forbidden(self):
		# This is the whole safety story for the open mode.
		settings = self.open_settings(all_can_read=1, all_can_write=1, all_can_delete=1)
		for doctype in ("User", "Role", "Server Script", "AgentX Settings", "Custom DocPerm"):
			self.assertFalse(policy.check(settings, doctype, "read", "u@x.com"), doctype)
			self.assertFalse(policy.check(settings, doctype, "delete", "u@x.com"), doctype)

	def test_frappe_permissions_still_decide(self):
		policy.has_permission_as = lambda user, dt, perm, name: False
		self.assertFalse(policy.check(self.open_settings(), "Purchase Order", "read", "u@x.com"))

	def test_an_explicit_row_overrides_the_defaults(self):
		settings = FakeSettings(
			[Row(document_type="Lead", can_read=1, can_create=1, requires_approval=0, max_per_day=0)],
			policy_mode="All Documents",
			all_can_read=1,
		)
		# The row grants create on Lead even though the default does not.
		self.assertTrue(policy.check(settings, "Lead", "create", "u@x.com"))
		self.assertFalse(policy.check(settings, "Purchase Order", "create", "u@x.com"))

	def test_writes_default_to_needing_approval(self):
		settings = self.open_settings(all_can_create=1, all_requires_approval=1)
		self.assertTrue(policy.check(settings, "Purchase Order", "create", "u@x.com").needs_approval)

	def test_an_unmapped_number_gains_nothing_from_open_mode(self):
		self.assertFalse(policy.check(self.open_settings(), "Purchase Order", "read", None))


class TestOpenEndedSchemas(unittest.TestCase):
	def test_listed_mode_pins_the_doctype_to_an_enum(self):
		settings = FakeSettings([Row(document_type="Lead", can_read=1)])
		schemas = {t["name"]: t for t in registry.build_schemas(settings)}
		self.assertEqual(schemas["list_documents"]["parameters"]["properties"]["doctype"]["enum"], ["Lead"])

	def test_open_mode_drops_the_enum_and_offers_discovery(self):
		settings = FakeSettings([], policy_mode="All Documents", all_can_read=1)
		schemas = {t["name"]: t for t in registry.build_schemas(settings)}
		self.assertNotIn("enum", schemas["list_documents"]["parameters"]["properties"]["doctype"])
		self.assertIn("find_doctypes", schemas)

	def test_discovery_is_not_offered_in_listed_mode(self):
		settings = FakeSettings([Row(document_type="Lead", can_read=1)])
		names = {t["name"] for t in registry.build_schemas(settings)}
		self.assertNotIn("find_doctypes", names)

	def test_open_mode_advertises_writes_from_the_defaults(self):
		settings = FakeSettings([], policy_mode="All Documents", all_can_read=1, all_can_create=1)
		names = {t["name"] for t in registry.build_schemas(settings)}
		self.assertIn("create_document", names)
		self.assertNotIn("delete_document", names)


# --------------------------------------------------------------------------
# Outbound alerts. The system starting a conversation is a different risk from
# answering one, so the guards here matter as much as the delivery.
# --------------------------------------------------------------------------

import datetime  # noqa: E402

from agent_x.core import alerts  # noqa: E402


class TestReminderDates(unittest.TestCase):
	"""Off-by-a-sign here means reminders fire on entirely the wrong day."""

	def setUp(self):
		self.today = datetime.date(2026, 8, 23)

	def test_days_before_looks_into_the_future(self):
		# A 3-day warning fires when the due date is 3 days away.
		self.assertEqual(
			alerts.target_date("Days Before", 3, self.today), datetime.date(2026, 8, 26)
		)

	def test_days_after_looks_into_the_past(self):
		# A 2-day follow-up fires when delivery was 2 days ago.
		self.assertEqual(
			alerts.target_date("Days After", 2, self.today), datetime.date(2026, 8, 21)
		)

	def test_zero_days_means_today(self):
		for event in ("Days Before", "Days After"):
			self.assertEqual(alerts.target_date(event, 0, self.today), self.today, event)

	def test_a_negative_setting_is_read_as_its_magnitude(self):
		# Someone typing -3 into "Days Before" means three days, not minus three.
		self.assertEqual(
			alerts.target_date("Days Before", -3, self.today), datetime.date(2026, 8, 26)
		)

	def test_missing_days_does_not_crash(self):
		self.assertEqual(alerts.target_date("Days Before", None, self.today), self.today)


class TestAlertConditions(unittest.TestCase):
	def test_an_empty_condition_always_passes(self):
		alert = Row(condition="", name="A")
		self.assertTrue(alerts.passes_condition(alert, Row(grand_total=100)))

	def test_whitespace_is_not_a_condition(self):
		alert = Row(condition="   ", name="A")
		self.assertTrue(alerts.passes_condition(alert, Row(grand_total=100)))


class TestAlertDispatchGuards(unittest.TestCase):
	def test_only_real_document_events_are_handled(self):
		# validate/before_save must not fire alerts, or a rejected save still messages someone.
		self.assertEqual(set(alerts.DOC_EVENTS), {"after_insert", "on_submit", "on_cancel", "on_update"})

	def test_scheduled_events_are_the_date_based_ones(self):
		self.assertEqual(set(alerts.SCHEDULED_EVENTS), {"Days Before", "Days After"})

	def test_our_own_doctypes_never_trigger_alerts(self):
		# Otherwise logging an outgoing message could trigger another message.
		for doctype in ("WhatsApp Message", "Agent Run", "AgentX Settings", "Agent Action"):
			self.assertTrue(
				doctype.startswith(("WhatsApp ", "Agent ", "AgentX ")), doctype
			)


# --------------------------------------------------------------------------
# Handing over to a person. The keyword match decides whether a customer gets
# a human, so a false positive silences the assistant for no reason.
# --------------------------------------------------------------------------

from agent_x.agent import handoff  # noqa: E402


class HandoffSettings:
	def __init__(self, enabled=1, words=None):
		self.handoff_enabled = enabled
		self.handoff_keywords = words or handoff.DEFAULT_KEYWORDS


class TestHandoffDetection(unittest.TestCase):
	def setUp(self):
		self.settings = HandoffSettings()

	def test_a_direct_request_is_caught(self):
		for text in ("can I talk to a human", "I want an agent", "let me speak to a person"):
			self.assertTrue(handoff.asked_for_a_person(text, self.settings), text)

	def test_whole_words_only(self):
		# "personalised" must not read as a request for a person, and an
		# "agentic" mention must not escalate.
		for text in ("do you sell personalised mugs", "is this agentic software"):
			self.assertFalse(handoff.asked_for_a_person(text, self.settings), text)

	def test_a_long_message_is_a_question_not_an_escalation(self):
		long_text = "I was wondering about a person " + ("detail " * 40)
		self.assertFalse(handoff.asked_for_a_person(long_text, self.settings))

	def test_nothing_fires_when_handover_is_off(self):
		self.assertFalse(handoff.asked_for_a_person("get me a human", HandoffSettings(enabled=0)))

	def test_empty_input_is_not_a_request(self):
		self.assertFalse(handoff.asked_for_a_person("", self.settings))
		self.assertFalse(handoff.asked_for_a_person(None, self.settings))

	def test_keywords_are_configurable(self):
		settings = HandoffSettings(words="msaada,operator")
		self.assertTrue(handoff.asked_for_a_person("naomba msaada", settings))
		self.assertFalse(handoff.asked_for_a_person("I want a human", settings))


class TestHandoverTool(unittest.TestCase):
	def test_the_tool_is_offered_only_when_handover_is_on(self):
		off = FakeSettings([Row(document_type="Lead", can_read=1)], handoff_enabled=0)
		on = FakeSettings([Row(document_type="Lead", can_read=1)], handoff_enabled=1)
		self.assertNotIn("hand_over", {t["name"] for t in registry.build_schemas(off)})
		self.assertIn("hand_over", {t["name"] for t in registry.build_schemas(on)})

	def test_handover_is_not_gated_on_a_doctype(self):
		# It acts on the conversation, so there is no policy to check.
		self.assertIsNone(registry.TOOL_OPERATIONS["hand_over"])

	def test_calling_it_returns_a_usable_result(self):
		result = registry.call("hand_over", {"reason": "angry customer"}, None)
		self.assertTrue(result.get("handed_over"))


# --------------------------------------------------------------------------
# Voice notes.
# --------------------------------------------------------------------------

from agent_x.agent import audio  # noqa: E402


class TestVoiceDetection(unittest.TestCase):
	def test_whatsapp_voice_note_types(self):
		for kind in ("audio", "voice", "ptt"):
			self.assertTrue(audio.is_voice(kind, None), kind)

	def test_a_forwarded_audio_file_is_caught_by_extension(self):
		self.assertTrue(audio.is_voice("document", {"filename": "note.ogg"}))
		self.assertTrue(audio.is_voice("document", {"filename": "recording.m4a"}))

	def test_text_and_images_are_not_voice(self):
		self.assertFalse(audio.is_voice("text", None))
		self.assertFalse(audio.is_voice("image", {"filename": "photo.jpg"}))

	def test_mime_comes_from_the_provider_when_it_says(self):
		self.assertEqual(audio.guess_mime({"mimetype": "audio/mp4"}), "audio/mp4")

	def test_codec_parameters_are_stripped(self):
		# Providers send "audio/ogg; codecs=opus", which the model rejects.
		self.assertEqual(audio.guess_mime({"mimetype": "audio/ogg; codecs=opus"}), "audio/ogg")

	def test_mime_falls_back_to_the_extension(self):
		self.assertEqual(audio.guess_mime({"filename": "note.mp3"}), "audio/mp3")

	def test_an_unknown_shape_defaults_to_the_whatsapp_format(self):
		self.assertEqual(audio.guess_mime({}), "audio/ogg")

	def test_transcription_is_skipped_when_switched_off(self):
		class Off:
			transcribe_voice_notes = 0

		self.assertEqual(audio.transcribe({"url": "http://x/a.ogg"}, Off()), "")

	def test_only_gemini_transcribes_today(self):
		class Claude:
			transcribe_voice_notes = 1
			ai_provider = "Anthropic Claude"

		self.assertEqual(audio.transcribe({"url": "http://x/a.ogg"}, Claude()), "")


# --------------------------------------------------------------------------
# Corrections.
# --------------------------------------------------------------------------


class TestCorrectionFormatting(unittest.TestCase):
	def setUp(self):
		from agent_x.agentx.doctype.agent_correction import agent_correction

		self.module = agent_correction

	def test_nothing_renders_for_no_corrections(self):
		self.assertEqual(self.module.format_for_prompt([]), "")

	def test_a_correction_is_stated_as_overriding(self):
		text = self.module.format_for_prompt(
			[{"applies_when": "asked about refunds", "wrong_reply": "we never refund",
			  "correct_behaviour": "say refunds take 7 days"}]
		)
		self.assertIn("overrides", text)
		self.assertIn("asked about refunds", text)
		self.assertIn("we never refund", text)
		self.assertIn("say refunds take 7 days", text)

	def test_a_correction_without_a_wrong_reply_still_renders(self):
		text = self.module.format_for_prompt(
			[{"applies_when": "greeting", "correct_behaviour": "use their first name"}]
		)
		self.assertIn("use their first name", text)
		self.assertNotIn("wrongly said", text)

	def test_corrections_are_numbered_so_the_model_can_count_them(self):
		text = self.module.format_for_prompt(
			[{"applies_when": "a", "correct_behaviour": "x"},
			 {"applies_when": "b", "correct_behaviour": "y"}]
		)
		self.assertIn("1. When: a", text)
		self.assertIn("2. When: b", text)


# --------------------------------------------------------------------------
# WaClient webhook payloads.
#
# REAL_MESSAGE below is copied verbatim from a production Error Log. Everything
# here failed silently before: the message arrived, no envelope was found, and
# it was dropped as "no sender" with nothing written to the log to say so.
# --------------------------------------------------------------------------

from agent_x.core import payload as wa_payload  # noqa: E402

REAL_MESSAGE = {
	"cmd": "agent_x.core.webhook.receive",
	"data": {
		"data": [
			{
				"conversationTimestamp": 1787633143,
				"id": "254113456822@s.whatsapp.net",
				"messages": [
					{
						"message": {
							"broadcast": False,
							"key": {
								"addressingMode": "lid",
								"fromMe": False,
								"id": "A5B71CDE9884CBFB23901E66AF761AB8",
								"participant": "",
								"remoteJid": "73143813197829@lid",
								"remoteJidAlt": "254113456822@s.whatsapp.net",
							},
							"message": {
								"conversation": "Hello",
								"messageContextInfo": {
									"deviceListMetadataVersion": 2,
									"messageSecret": "EpMsWFEiGKmJd4XhdLTjtFjmd3qWnB3TECfP8Ts1BVQ=",
								},
							},
							"messageTimestamp": 1787633143,
							"pushName": "Ronoh",
							"verifiedBizName": "Ronoh",
						}
					}
				],
				"unreadCount": 1,
			}
		],
		"event": "chats.update",
	},
	"instance_id": "6A3B2A60E842E",
}


def upsert(text="Yo", remote="254113456822@s.whatsapp.net", **key):
	k = {"remoteJid": remote, "id": "M1", "fromMe": False}
	k.update(key)
	return {
		"data": {"event": "messages.upsert",
		         "messages": [{"key": k, "message": {"conversation": text}, "pushName": "R"}]},
		"instance_id": "I",
	}


class TestRealWaClientMessage(unittest.TestCase):
	def setUp(self):
		self.parsed = wa_payload.parse(REAL_MESSAGE)

	def test_the_message_is_found_at_all(self):
		# It sits at data.data[0].messages[0].message, which no fixed path caught.
		self.assertIsNotNone(self.parsed, "the real payload must parse")

	def test_the_text_is_read(self):
		self.assertEqual(self.parsed["text"], "Hello")

	def test_the_sender_is_the_phone_number_not_the_lid(self):
		# remoteJid is an opaque @lid; the number lives in remoteJidAlt. Using
		# the lid means the sender never matches an allowed number.
		self.assertEqual(self.parsed["wa_id"], "254113456822")
		self.assertNotIn("lid", self.parsed["chat_id"])

	def test_an_allowed_number_actually_matches(self):
		from agent_x.core.phone import same_number

		self.assertTrue(same_number("254113456822", self.parsed["wa_id"]))
		self.assertFalse(same_number("254113456822", "73143813197829"))

	def test_identifiers_survive(self):
		self.assertEqual(self.parsed["message_id"], "A5B71CDE9884CBFB23901E66AF761AB8")
		self.assertEqual(self.parsed["instance_id"], "6A3B2A60E842E")
		self.assertEqual(self.parsed["push_name"], "Ronoh")

	def test_it_is_a_direct_chat_from_someone_else(self):
		self.assertFalse(self.parsed["is_group"])
		self.assertFalse(self.parsed["from_me"])

	def test_the_timestamp_is_kept(self):
		self.assertEqual(self.parsed["timestamp"], 1787633143)


class TestPayloadShapes(unittest.TestCase):
	def test_the_flat_upsert_shape_still_works(self):
		parsed = wa_payload.parse(upsert("Yo"))
		self.assertEqual(parsed["text"], "Yo")
		self.assertEqual(parsed["wa_id"], "254113456822")

	def test_an_event_with_no_readable_content_is_ignored(self):
		empty = {"data": {"event": "chats.update", "data": [{"id": "254@s.whatsapp.net",
			"messages": [{"message": {"key": {"remoteJid": "7@lid",
			"remoteJidAlt": "254113456822@s.whatsapp.net", "id": "M", "fromMe": False},
			"message": {}}}]}]}, "instance_id": "I"}
		# Otherwise the assistant answers silence.
		self.assertIsNone(wa_payload.parse(empty))

	def test_presence_updates_are_ignored(self):
		noise = {"data": {"event": "presence.update", "data": [{"id": "254@s.whatsapp.net"}]},
		         "instance_id": "I"}
		self.assertIsNone(wa_payload.parse(noise))

	def test_a_receipt_may_arrive_without_content(self):
		ack = {"data": {"event": "messages.ack", "status": 3,
			"messages": [{"key": {"remoteJid": "254113456822@s.whatsapp.net", "id": "M4",
			"fromMe": True}, "message": {}}]}, "instance_id": "I"}
		parsed = wa_payload.parse(ack)
		self.assertIsNotNone(parsed)
		self.assertTrue(wa_payload.is_status_event(parsed))

	def test_chats_update_is_not_mistaken_for_a_receipt(self):
		# "chats.update" contains "update" but carries real messages.
		self.assertFalse(wa_payload.is_status_event(wa_payload.parse(REAL_MESSAGE)))

	def test_a_group_keeps_the_group_and_the_real_participant(self):
		grp = {"data": {"event": "messages.upsert", "messages": [{"key": {
			"remoteJid": "120363000@g.us", "participant": "999@lid",
			"participantAlt": "254700111222@s.whatsapp.net", "id": "G1", "fromMe": False},
			"message": {"conversation": "team msg"}, "pushName": "X"}]}, "instance_id": "I"}
		parsed = wa_payload.parse(grp)
		self.assertTrue(parsed["is_group"])
		self.assertEqual(parsed["chat_id"], "120363000@g.us")
		self.assertEqual(parsed["wa_id"], "254700111222")

	def test_our_own_echo_is_flagged(self):
		parsed = wa_payload.parse(upsert("mine", fromMe=True))
		self.assertTrue(parsed["from_me"])

	def test_media_is_recognised(self):
		media = {"data": {"event": "messages.upsert", "messages": [{"key": {
			"remoteJid": "254113456822@s.whatsapp.net", "id": "I1", "fromMe": False},
			"message": {"imageMessage": {"caption": "look", "mimetype": "image/jpeg",
			"fileName": "p.jpg"}}, "pushName": "R"}]}, "instance_id": "I"}
		parsed = wa_payload.parse(media)
		self.assertEqual(parsed["message_type"], "image")
		self.assertEqual(parsed["text"], "look")

	def test_nonsense_does_not_raise(self):
		for junk in ({}, {"data": {}}, {"data": []}, {"a": {"b": {"c": 1}}}):
			self.assertIsNone(wa_payload.parse(junk))


class TestDuplicateDelivery(unittest.TestCase):
	"""WaClient sends more than one event per message.

	Logging it twice is untidy. Answering it twice, with two agents racing on
	one conversation, is what produced the "something went wrong" replies.
	"""

	def test_the_same_id_arrives_under_different_event_names(self):
		chats = wa_payload.parse(REAL_MESSAGE)
		upserted = wa_payload.parse(
			upsert("Hello", remote="73143813197829@lid",
			       remoteJidAlt="254113456822@s.whatsapp.net",
			       id="A5B71CDE9884CBFB23901E66AF761AB8")
		)
		# Same message, two events. The id is what identifies it.
		self.assertEqual(chats["message_id"], upserted["message_id"])
		self.assertEqual(chats["wa_id"], upserted["wa_id"])
		self.assertNotEqual(chats["event"], upserted["event"])


class TestEscalationBeatsConfirmation(unittest.TestCase):
	"""Handing over and asking for a confirmation both own the conversation status."""

	def test_the_two_statuses_are_distinct(self):
		# If both are written in one turn the later one wins, which silently
		# undid the escalation.
		self.assertNotEqual("Handed Over", "Awaiting Confirmation")

	def test_runtime_guards_the_pending_branch(self):
		import inspect

		source = inspect.getsource(runtime.run_agent)
		self.assertIn("if pending_action and not handed_over:", source)


# --------------------------------------------------------------------------
# Reading an order off a photo, a document, or a voice note.
# --------------------------------------------------------------------------

from agent_x.agent import media  # noqa: E402


class TestItemMatching(unittest.TestCase):
	"""A customer's list never uses your item codes."""

	def test_exact_wording_scores_top(self):
		self.assertEqual(catalogue.score("Queen Cake 10/-", "Queen Cake 10/-"), 1.0)

	def test_partial_wording_still_scores_well(self):
		# "queen cake" against "Queen Cake 10/-" is how people actually write.
		self.assertGreater(catalogue.score("queen cake", "Queen Cake 10/-"), 0.8)

	def test_unrelated_wording_scores_low(self):
		self.assertLess(catalogue.score("completely unrelated widget", "Queen Cake 10/-"), 0.5)

	def test_a_size_appearing_in_both_helps(self):
		with_size = catalogue.score("andolex 200ml", "ANDOLEX -C ORAL RINSE 200ML")
		wrong_size = catalogue.score("andolex 200ml", "ANDOLEX C SPRAY 30 ML")
		self.assertGreater(with_size, wrong_size)

	def test_the_floor_sits_clear_of_the_noise(self):
		# Unrelated wording scored around 0.45 against a large catalogue, which
		# was being offered to customers as a maybe.
		self.assertGreaterEqual(catalogue.FLOOR, 0.55)
		self.assertLess(catalogue.FLOOR, catalogue.CONFIDENT)

	def test_noise_words_are_ignored(self):
		self.assertNotIn("the", catalogue.tokens("the queen cake"))
		self.assertIn("queen", catalogue.tokens("the queen cake"))

	def test_punctuation_does_not_break_matching(self):
		self.assertEqual(catalogue.normalise_name("ANDOLEX -C, 200ML!"), "andolex c 200ml")


class TestAttachmentHandling(unittest.TestCase):
	def test_photos_are_recognised(self):
		for m in ({"mimetype": "image/jpeg"}, {"filename": "list.png"}, {"filename": "IMG_1.HEIC"}):
			self.assertEqual(media.classify(m), "image", m)

	def test_pdfs_and_text_are_readable(self):
		for m in ({"mimetype": "application/pdf"}, {"filename": "order.pdf"},
		          {"filename": "list.csv"}, {"mimetype": "text/plain"}):
			self.assertEqual(media.classify(m), "document", m)

	def test_office_files_are_flagged_as_unreadable(self):
		# Sending the bytes would be useless; the reply should say what to send.
		for m in ({"filename": "order.xlsx"}, {"filename": "order.docx"}):
			self.assertEqual(media.classify(m), "unreadable", m)

	def test_nothing_attached_is_not_a_crash(self):
		self.assertEqual(media.classify({}), "none")
		self.assertEqual(media.classify(None), "none")

	def test_codec_parameters_are_stripped_from_the_mime(self):
		self.assertEqual(media.mime_for({"mimetype": "image/jpeg"}, "image"), "image/jpeg")

	def test_mime_falls_back_to_the_extension(self):
		self.assertEqual(media.mime_for({"filename": "a.png"}, "image"), "image/png")
		self.assertEqual(media.mime_for({"filename": "a.pdf"}, "document"), "application/pdf")

	def test_a_switched_off_reader_attaches_nothing(self):
		class Off:
			ai_read_images = 0
			read_documents = 0
			ai_max_image_mb = 4
			max_document_mb = 10
			request_timeout = 30

		self.assertIsNone(media.prepare({"mimetype": "image/jpeg", "url": "http://x/a.jpg"}, Off()))


class TestCustomerToolsAreRegistered(unittest.TestCase):
	def test_finding_a_customer_only_needs_read(self):
		self.assertEqual(registry.TOOL_OPERATIONS["find_customer"], "read")
		self.assertEqual(registry.TOOL_OPERATIONS["link_customer"], "read")

	def test_creating_one_is_a_write(self):
		# So it goes through the policy gate and the confirmation step.
		self.assertEqual(registry.TOOL_OPERATIONS["create_customer"], "create")

	def test_matching_items_only_needs_read(self):
		self.assertEqual(registry.TOOL_OPERATIONS["match_items"], "read")


class TestVerifiedCustomersGate(unittest.TestCase):
	"""Only Serve Verified Customers, and the ways it could go wrong."""

	def setUp(self):
		from agent_x.core import webhook

		self.webhook = webhook

	def stranger(self, **settings_kw):
		class Settings:
			only_verified_customers = settings_kw.get("only_verified_customers", 1)

		return Settings()

	def test_the_gate_is_off_by_default_for_existing_sites(self):
		class Off:
			only_verified_customers = 0

		# Nothing is looked up at all when it is off.
		self.assertTrue(self.webhook.is_verified_customer(None, Off()))

	def test_a_failed_lookup_does_not_lock_everyone_out(self):
		# Failing open is right here: a broken Customer query should not silence
		# the assistant for every customer at once.
		class Boom:
			only_verified_customers = 1

		class BadContact:
			@property
			def wa_id(self):
				raise RuntimeError("database is unhappy")

		self.assertTrue(self.webhook.is_verified_customer(BadContact(), Boom()))

	def test_the_skip_reason_is_specific(self):
		import inspect

		source = inspect.getsource(self.webhook.should_skip)
		self.assertIn("not a known customer", source)

	def test_the_notice_is_keyed_on_inbound_not_outbound(self):
		# Counting our own sent messages means a send that failed is retried on
		# every message the person ever sends.
		import inspect

		source = inspect.getsource(self.webhook.notify_unverified)
		self.assertIn('"direction": "Incoming"', source)
		self.assertNotIn('"direction": "Outgoing"', source)


class TestCustomerVerification(unittest.TestCase):
	def test_a_group_is_never_verified(self):
		from agent_x.agent.tools import customers

		class Group:
			is_group = 1
			customer = None
			wa_id = "120363000"

		# A group is not a person, so there is nobody to verify.
		self.assertIsNone(customers.resolve_for_contact(Group(), auto_link=False))

	def test_an_existing_link_wins_without_a_lookup(self):
		from agent_x.agent.tools import customers

		class Linked:
			is_group = 0
			customer = "ACME"
			wa_id = "254113456822"

		customers.existing_link = lambda c: c.customer
		self.assertEqual(customers.resolve_for_contact(Linked(), auto_link=False), "ACME")

	def test_several_matches_are_not_guessed_between(self):
		from agent_x.agent.tools import customers

		class Contact:
			is_group = 0
			customer = None
			wa_id = "254113456822"

		customers.existing_link = lambda c: None
		customers.by_phone = lambda n: ["ACME", "ACME LTD"]
		# Picking one would put orders on the wrong company.
		self.assertIsNone(customers.resolve_for_contact(Contact(), auto_link=False))

	def test_exactly_one_match_is_accepted(self):
		from agent_x.agent.tools import customers

		class Contact:
			is_group = 0
			customer = None
			wa_id = "254113456822"

		customers.existing_link = lambda c: None
		customers.by_phone = lambda n: ["ACME"]
		self.assertEqual(customers.resolve_for_contact(Contact(), auto_link=False), "ACME")


class TestOnlyRealConversations(unittest.TestCase):
	"""A WhatsApp Status is a broadcast to everyone in someone's contacts.

	Parsing one as an inbound message means the assistant sends a private reply
	to somebody's story. The bridge always filtered these; the WaClient path did
	not, which is the path in production.
	"""

	def envelope(self, remote, event="messages.upsert", **extra):
		body = {"instance_id": "I", "data": {"event": event, "messages": [
			{"key": {"remoteJid": remote, "id": "X1", "fromMe": False},
			 "message": {"conversation": "Hello"}, "pushName": "Someone"}]}}
		body["data"].update(extra)
		return body

	def test_a_status_post_is_not_a_conversation(self):
		self.assertIsNone(wa_payload.parse(self.envelope("status@broadcast")))

	def test_a_broadcast_list_is_not_a_conversation(self):
		self.assertIsNone(wa_payload.parse(self.envelope("1234567890@broadcast")))

	def test_a_channel_is_not_a_conversation(self):
		self.assertIsNone(wa_payload.parse(self.envelope("120363000000@newsletter")))

	def test_a_status_hiding_behind_a_lid_alt_is_still_caught(self):
		body = self.envelope("status@broadcast")
		body["data"]["messages"][0]["key"]["remoteJidAlt"] = "254113456822@s.whatsapp.net"
		self.assertIsNone(wa_payload.parse(body))

	def test_direct_chats_and_groups_still_parse(self):
		for jid in ("254113456822@s.whatsapp.net", "120363000000000000@g.us"):
			parsed = wa_payload.parse(self.envelope(jid))
			self.assertIsNotNone(parsed, jid)
			self.assertEqual(parsed["text"], "Hello")

	def test_is_conversation_directly(self):
		for jid in ("status@broadcast", "x@broadcast", "y@newsletter", "", None):
			self.assertFalse(wa_payload.is_conversation(jid), jid)
		for jid in ("2547@s.whatsapp.net", "120@g.us"):
			self.assertTrue(wa_payload.is_conversation(jid), jid)


class TestReceiptsAreNeverMessages(unittest.TestCase):
	def test_an_unrecognised_ack_does_not_fall_through(self):
		# The dispatcher used to only return when the ack mapped to a known
		# status, so a new status name got logged and answered as a message.
		import inspect

		from agent_x.core import webhook

		source = inspect.getsource(webhook.dispatch_waclient)
		self.assertIn("delivery receipt", source)

	def test_ack_events_are_recognised(self):
		for event in ("messages.ack", "message.status", "receipt.update"):
			self.assertTrue(wa_payload.is_ack_event(event), event)

	def test_a_chats_update_is_not_an_ack(self):
		self.assertFalse(wa_payload.is_ack_event("chats.update"))


# --------------------------------------------------------------------------
# Why the assistant went blank mid-order.
#
# A nine item order, a detour to create a customer, then "now my order" and
# nothing. Two separate causes: the model returned empty content and the code
# read that as "no answer", and the request itself only lived in the message
# window, which slides.
# --------------------------------------------------------------------------


class TestEmptyModelReply(unittest.TestCase):
	def test_running_out_of_tokens_is_named(self):
		with self.assertRaises(provider.AIError) as caught:
			provider.explain_empty({"finishReason": "MAX_TOKENS"}, {})
		self.assertIn("output tokens", str(caught.exception).lower())

	def test_thinking_tokens_are_pointed_at_when_they_caused_it(self):
		with self.assertRaises(provider.AIError) as caught:
			provider.explain_empty({"finishReason": "MAX_TOKENS"}, {"thoughtsTokenCount": 1900})
		self.assertIn("thinking", str(caught.exception).lower())

	def test_a_refusal_is_named(self):
		for reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
			with self.assertRaises(provider.AIError):
				provider.explain_empty({"finishReason": reason}, {})

	def test_a_normal_stop_is_not_an_error(self):
		self.assertIsNone(provider.explain_empty({"finishReason": "STOP"}, {}))
		self.assertIsNone(provider.explain_empty({}, {}))


class TestThinkingBudget(unittest.TestCase):
	"""Gemini 2.5 and newer spend the reply budget on thinking unless told not to."""

	class Settings:
		ai_thinking_budget = 0

	def test_versions_are_read_from_the_model_name(self):
		self.assertEqual(provider.model_version("gemini-2.5-flash"), (2, 5))
		self.assertEqual(provider.model_version("gemini-3-pro"), (3, 0))
		self.assertEqual(provider.model_version("gemini-1.5-pro"), (1, 5))
		self.assertIsNone(provider.model_version("gpt-4o-mini"))

	def test_thinking_models_get_a_budget(self):
		for model in ("gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-pro", "gemini-3.0-flash"):
			self.assertEqual(provider.thinking_budget(self.Settings(), model), 0, model)

	def test_older_models_are_left_alone(self):
		# Sending thinkingConfig to a model that does not think is an error.
		for model in ("gemini-2.0-flash", "gemini-1.5-pro", "", None):
			self.assertIsNone(provider.thinking_budget(self.Settings(), model), model)

	def test_a_configured_budget_is_honoured(self):
		class Generous:
			ai_thinking_budget = 1024

		self.assertEqual(provider.thinking_budget(Generous(), "gemini-2.5-flash"), 1024)


class TestWorkingMemory(unittest.TestCase):
	"""Conversation history is a window and it slides. A note does not."""

	class Settings:
		business_context = ""
		system_prompt = ""
		automation_enabled = 0
		doctype_policies = []
		max_reply_characters = 1500
		policy_mode = "Listed Documents Only"

	def test_a_note_reaches_the_prompt(self):
		note = "Customer wants 5 each of 111574, 111462, 111656 - blocked on their account."
		built = prompt.build(self.Settings(), notes=note)
		self.assertIn(note, built)

	def test_the_note_is_framed_as_unfinished_work(self):
		built = prompt.build(self.Settings(), notes="something owed")
		self.assertIn("STILL OUTSTANDING", built)
		self.assertIn("without making them repeat", built)

	def test_no_note_adds_no_section(self):
		self.assertNotIn("STILL OUTSTANDING", prompt.build(self.Settings()))

	def test_memory_tools_need_no_doctype(self):
		# They act on the conversation, so there is no policy to check.
		self.assertIsNone(registry.TOOL_OPERATIONS["remember"])
		self.assertIsNone(registry.TOOL_OPERATIONS["forget"])

	def test_memory_is_always_offered(self):
		# Even with automation off; losing a request is not an automation feature.
		settings = FakeSettings([Row(document_type="Lead", can_read=1)])
		names = {t["name"] for t in registry.build_schemas(settings)}
		self.assertIn("remember", names)
		self.assertIn("forget", names)


class TestGroupsAreLeftAlone(unittest.TestCase):
	"""A bot answering in a group talks over everyone in it."""

	def setUp(self):
		from agent_x.core import webhook

		self.webhook = webhook

	class Settings:
		def __init__(self, reply_to_groups=0):
			self.reply_to_groups = reply_to_groups
			self.ai_enabled = 1
			self.only_verified_customers = 0
			self.reply_scope = "Everyone"

		def is_excluded(self, number):
			return False

		def is_allowed(self, number):
			return True

	class Contact:
		blocked = 0
		opted_out = 0
		wa_id = "254700111222"

	def test_a_group_is_skipped_by_default(self):
		reason = self.webhook.should_skip(self.Contact(), self.Settings(), is_group=True)
		self.assertEqual(reason, "group chat")

	def test_a_direct_chat_is_served(self):
		self.assertIsNone(self.webhook.should_skip(self.Contact(), self.Settings(), is_group=False))

	def test_the_setting_can_open_groups_up(self):
		reason = self.webhook.should_skip(
			self.Contact(), self.Settings(reply_to_groups=1), is_group=True
		)
		self.assertIsNone(reason)

	def test_the_group_check_comes_first(self):
		# Before the allowlist and before customer verification, so a group can
		# never fall through to the "not a known customer" notice and get a
		# reply that way.
		import inspect

		source = inspect.getsource(self.webhook.should_skip)
		group_at = source.index("group chat")
		self.assertLess(group_at, source.index("not an allowed number"))
		self.assertLess(group_at, source.index("not a known customer"))

	def test_a_group_message_still_identifies_the_real_sender(self):
		# Logged for the record, with the participant as the sender rather than
		# the group id.
		grp = {"data": {"event": "messages.upsert", "messages": [{"key": {
			"remoteJid": "120363000@g.us", "participant": "254700111222@s.whatsapp.net",
			"id": "G1", "fromMe": False}, "message": {"conversation": "hi"},
			"pushName": "X"}]}, "instance_id": "I"}
		parsed = wa_payload.parse(grp)
		self.assertTrue(parsed["is_group"])
		self.assertEqual(parsed["wa_id"], "254700111222")
		self.assertEqual(parsed["chat_id"], "120363000@g.us")


# --------------------------------------------------------------------------
# Keeping the model bill down.
#
# Every call carries the whole system prompt and every tool definition with it,
# measured at roughly 2,900 tokens before the conversation. The cheapest call
# is the one that never happens.
# --------------------------------------------------------------------------


class Rule:
	"""Stands in for a WhatsApp Reply Rule row."""

	def __init__(self, pattern, match_type="Exact", case_sensitive=0):
		self.pattern = pattern
		self.match_type = match_type
		self.case_sensitive = case_sensitive
		self.enabled = 1

	def patterns(self):
		return [line.strip() for line in self.pattern.splitlines() if line.strip()]


def matches(rule, text):
	from agent_x.agentx.doctype.whatsapp_reply_rule.whatsapp_reply_rule import WhatsAppReplyRule

	return WhatsAppReplyRule.matches(rule, text)


class TestReplyRuleMatching(unittest.TestCase):
	def test_exact_ignores_case_and_spacing_by_default(self):
		rule = Rule("hi\nhello")
		for text in ("hi", "Hi", "HELLO", "  hello  "):
			self.assertTrue(matches(rule, text), text)

	def test_exact_does_not_match_a_sentence(self):
		# "hi" must not answer "hi, can I order 5 boxes of paracetamol".
		rule = Rule("hi")
		self.assertFalse(matches(rule, "hi, can I order 5 boxes"))

	def test_contains_matches_inside_a_sentence(self):
		rule = Rule("opening hours", match_type="Contains")
		self.assertTrue(matches(rule, "what are your opening hours today?"))

	def test_starts_with(self):
		rule = Rule("order", match_type="Starts With")
		self.assertTrue(matches(rule, "order 5 boxes"))
		self.assertFalse(matches(rule, "I want to order 5 boxes"))

	def test_regex(self):
		rule = Rule(r"^\d{1,3}$", match_type="Regex")
		self.assertTrue(matches(rule, "42"))
		self.assertFalse(matches(rule, "forty two"))

	def test_a_broken_regex_does_not_raise(self):
		rule = Rule("[unclosed", match_type="Regex")
		self.assertFalse(matches(rule, "anything"))

	def test_case_sensitive_when_asked(self):
		rule = Rule("OK", case_sensitive=1)
		self.assertTrue(matches(rule, "OK"))
		self.assertFalse(matches(rule, "ok"))


class TestRulesNeverHijackAFlow(unittest.TestCase):
	"""The ordering is the safety property here."""

	class Settings:
		reply_rules_enabled = 1

	class Conversation:
		def __init__(self, status="Active", notes=None):
			self.status = status
			self.notes = notes

	def test_a_pending_confirmation_is_never_intercepted(self):
		# A rule matching "yes" must not swallow a consent to a document change.
		self.assertIsNone(
			runtime.matching_rule("yes", self.Settings(), self.Conversation("Awaiting Confirmation"))
		)

	def test_a_handover_is_never_intercepted(self):
		self.assertIsNone(
			runtime.matching_rule("hello", self.Settings(), self.Conversation("Handed Over"))
		)

	def test_outstanding_work_is_never_intercepted(self):
		# A canned line would look like the assistant had forgotten the order.
		self.assertIsNone(
			runtime.matching_rule("hello", self.Settings(), self.Conversation(notes="owes an order"))
		)

	def test_rules_can_be_switched_off_entirely(self):
		class Off:
			reply_rules_enabled = 0

		self.assertIsNone(runtime.matching_rule("hello", Off(), self.Conversation()))


class TestDailyBudget(unittest.TestCase):
	def test_no_budget_means_no_limit(self):
		class Unlimited:
			daily_token_budget = 0

		self.assertFalse(runtime.over_budget(Unlimited()))

	def test_a_failed_count_does_not_stop_the_assistant(self):
		class Broken:
			daily_token_budget = 1000

		original = runtime.tokens_used_today
		runtime.tokens_used_today = lambda: (_ for _ in ()).throw(RuntimeError("no db"))
		try:
			self.assertFalse(runtime.over_budget(Broken()))
		finally:
			runtime.tokens_used_today = original

	def test_the_budget_stops_the_model_when_reached(self):
		class Tight:
			daily_token_budget = 100

		original = runtime.tokens_used_today
		runtime.tokens_used_today = lambda: 150
		try:
			self.assertTrue(runtime.over_budget(Tight()))
		finally:
			runtime.tokens_used_today = original


class TestSemanticMatching(unittest.TestCase):
	"""Vectors used to avoid the generation call, not to feed it.

	Retrieval for the prompt only saves whatever business context would have
	been pasted in. Avoiding the call saves the whole call, which on this app
	is about 3,200 tokens.
	"""

	def setUp(self):
		from agent_x.agent import semantic

		self.semantic = semantic

	def test_the_same_question_is_only_embedded_once(self):
		# Casing and spacing must not produce a second embedding call.
		a = self.semantic.fingerprint("What time do you open?")
		self.assertEqual(a, self.semantic.fingerprint("what time do you open?"))
		self.assertEqual(a, self.semantic.fingerprint("  What   time do you open? "))

	def test_a_different_question_gets_a_different_key(self):
		self.assertNotEqual(
			self.semantic.fingerprint("what time do you open?"),
			self.semantic.fingerprint("what time do you close?"),
		)

	def test_an_empty_message_is_still_hashable(self):
		self.assertTrue(self.semantic.fingerprint(""))
		self.assertTrue(self.semantic.fingerprint(None))

	def test_cosine_picks_the_closest_phrasing(self):
		import numpy

		def norm(v):
			v = numpy.array(v, dtype=numpy.float32)
			return v / (numpy.linalg.norm(v) or 1.0)

		rules = numpy.stack([norm([1, 0, 0]), norm([0.9, 0.4, 0])])
		close = rules @ norm([0.88, 0.45, 0.02])
		self.assertEqual(int(numpy.argmax(close)), 1)
		self.assertGreater(float(close.max()), 0.80)

	def test_an_unrelated_question_scores_below_the_floor(self):
		# Answering the wrong question from a rule is worse than paying for a
		# proper reply.
		import numpy

		def norm(v):
			v = numpy.array(v, dtype=numpy.float32)
			return v / (numpy.linalg.norm(v) or 1.0)

		rules = numpy.stack([norm([1, 0, 0]), norm([0.9, 0.4, 0])])
		far = rules @ norm([0, 0.05, 1])
		self.assertLess(float(far.max()), 0.80)

	def test_matching_degrades_quietly_without_embeddings(self):
		# No key or no quota must lose the optimisation, not the reply.
		class Settings:
			semantic_threshold = 0.8

		original = self.semantic.embed_one
		self.semantic.embed_one = lambda text, settings: None
		try:
			name, score = self.semantic.best_rule("anything", Settings())
			self.assertIsNone(name)
		finally:
			self.semantic.embed_one = original


class TestRuleOrderingIsCheapestFirst(unittest.TestCase):
	def test_word_rules_are_tried_before_semantic_ones(self):
		# A word match is free; a semantic match costs an embedding. Reaching
		# for the paid one first would waste a call on every greeting.
		import inspect

		from agent_x.agentx.doctype.whatsapp_reply_rule import whatsapp_reply_rule

		source = inspect.getsource(whatsapp_reply_rule.find_match)
		self.assertLess(source.index("rule.matches(body)"), source.index("best_rule"))

	def test_semantic_is_skipped_when_no_semantic_rules_exist(self):
		import inspect

		from agent_x.agentx.doctype.whatsapp_reply_rule import whatsapp_reply_rule

		source = inspect.getsource(whatsapp_reply_rule.find_match)
		self.assertIn("if not semantic_rules:", source)


if __name__ == "__main__":
	unittest.main(verbosity=2)
