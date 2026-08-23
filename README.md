# AgentX

An ERPNext automation agent that talks over WhatsApp. An AI assistant answers
messages and — within limits you set — reads and changes documents in the system.

## Choosing a provider

WhatsApp Web needs a socket that stays open for days. Frappe's workers are
forked and short-lived, so that socket has to live *somewhere else*. Where you
put it is the only real deployment decision, and AgentX supports both answers.

| | **WaClient** | **Self-hosted bridge** |
| --- | --- | --- |
| Works on Frappe Cloud | **Yes** | No |
| Extra infrastructure | None | A server running Node |
| Who holds the session | WaClient | You |
| Who can read messages | WaClient | Only you |
| Cost | Their subscription | A small VPS |
| QR scanned in Desk | Yes | Yes |

**On Frappe Cloud, use WaClient.** There is nowhere on a managed bench to run a
persistent process, so the session has to be hosted. Everything else — the
agent, the policy gate, the audit trail, the QR in Desk — is identical either
way, and switching later is a dropdown in AgentX Settings, not a rewrite.

```
                  ┌── WaClient (hosted)  ──┐
WhatsApp  <-->    │                        │  <-->  Frappe (agent_x)
                  └── bridge/ (your box) ──┘         policy, agent, audit
```

## Setup

### 1. Install

```bash
bench --site <your-site> install-app agent_x
bench --site <your-site> migrate
```

### 2. Point AgentX at a provider

Open **AgentX Settings → Connection** and pick one.

**WaClient** — paste your Access Token. Create an instance in the WaClient
dashboard and note its Instance ID. Set a Webhook Token (any long random
string), save, then press **Register Webhook**.

**Self-hosted bridge** — see [bridge/README.md](bridge/README.md), then fill in
the Bridge URL, API Token, and Webhook Secret. Copy the read-only **Webhook
URL** into the bridge's `BRIDGE_WEBHOOK_URL` and restart it.

Press **Test Connection** either way.

### 3. Configure the assistant

| Tab | What to set |
| --- | --- |
| AI Assistant | API key. Gemini and `gemini-2.5-flash` are the defaults. Fill in Business Context. Press **Test AI**. |
| Access | Your own number under Allowed Numbers, with an **Acts As User**. |
| Automation | Leave off until you have replies working. |

### 4. Pair a number

Create a **WhatsApp Session**. On WaClient, paste the Instance ID — or press
**Create Instance** and AgentX mints one for you. Then press **Connect** and
scan the QR that appears in the form.

If scanning is awkward, **Use Pairing Code** gives you an eight character code
to type into WhatsApp under Linked Devices instead.

The bridge pushes its QR over realtime. WaClient has no push, so the form polls
every five seconds while a QR is on screen and stops as soon as it connects.

## What the assistant can do

Reading: `list_documents`, `get_document`, `count_documents`, `describe_doctype`.
Writing: `create_document`, `update_document`, `submit_document`,
`cancel_document`, `delete_document`.

Every tool offered to the model is derived from the policy table, so a document
type you have not listed is not merely refused — the model is never told it
exists.

## The safety model

Three gates, and a change has to pass all of them.

**1. Policy.** You list each document type in AgentX Settings and tick the
operations allowed on it. A fixed set — `User`, `Role`, `Server Script`,
`AgentX Settings` and friends — can never be listed at all, because they grant
access or hold secrets.

**2. Permissions.** Every action runs as a real Frappe user, mapped from the
sender's phone number. `frappe.set_user` swaps the identity for the duration of
the write, so roles, user permissions, and ownership apply exactly as they would
in Desk. Nobody gains anything over WhatsApp that they lack in the app.

**3. Confirmation.** With **Confirm Before Writing** on, the change is described
back to the sender and waits for a clear `YES`. Anything ambiguous — including
*"yes but only if…"* — is treated as **not** consent, and asks again.
Unanswered confirmations expire.

Also: per-doctype daily caps, a per-conversation action limit, an optional field
allowlist, and a **Dry Run** switch that plans and logs everything without
writing.

## The audit trail

Every turn writes an **Agent Run**: the incoming message, the reply, each tool
call with its arguments, tokens used, and duration. Every document change writes
an **Agent Action** recording what was asked, who it ran as, whether a human
approved, and what happened. Nothing is written without one.

Pending actions can be approved from Desk, where the approver must hold the
permission the action needs — so approval cannot launder a change past a
permission check.

## WaClient endpoints in use

Built against [the WhatsApp Web API docs](https://waclient.com/docs/whatsapp-web-api).
Everything is JSON on `https://api.waclient.com`, with `instance_id` and
`access_token` added to every call.

| Purpose | Endpoint | Where it surfaces |
| --- | --- | --- |
| Pairing QR | `get_qrcode`, `relogin_qrcode` | Connect on a WhatsApp Session |
| Pairing code | `get_paircode`, `relogin_paircode` | **Use Pairing Code** button |
| Create an instance | `create_instance` | **Create Instance** button |
| Connection state | `instance_status`, `instance_info` | Status polling, Test Connection |
| Unlink | `logout`, `reconnect`, `delete_instance` | Session buttons |
| Webhook | `set_webhook`, `get_webhook` | **Register Webhook** |
| Send | `send` (text, link, media, location, live_location, poll) | `agent_x.api.*` |
| Blue ticks | `mark_message_read` | Automatic on inbound |
| Typing indicator | `send_chat_presence` | Automatic while the agent thinks |
| Reactions | `react_to_message` | `agent_x.api.react` |
| Remove / forward | `delete_message`, `forward_message` | Transport methods |
| Number validation | `check_number`, `check_exist` | `agent_x.api.check_numbers` |
| Reading the account | `get_chats`, `get_groups`, `get_messages_by_chat` | `agent_x.api.get_chats` / `get_groups` |

Two of these are on by default and worth knowing about: the assistant marks an
incoming message read and shows a typing indicator while it works, so a slow
answer does not look like silence. Both are switches under **Conversation
Manners**.

Anything a provider cannot do returns `{"supported": false}` rather than
raising, so the same call is safe against either provider.

## Sending from your own code

```python
frappe.call("agent_x.api.send_text", to="254712345678", message="Your order shipped.")
```

```python
from agent_x.core.messaging import send_message

send_message(
    "254712345678",
    "Invoice attached.",
    media_url="https://example.com/inv.pdf",
    media_kind="document",
    reference_doctype="Sales Invoice",
    reference_name="ACC-SINV-2026-00001",
)
```

WaClient can only send media from a public URL; the bridge also accepts raw
bytes.

## Tests

```bash
python3 tests/test_logic.py        # 83 tests, no site or database needed
```

They stub Frappe, so the phone handling, provider payload shaping, policy gate,
and confirmation parser can be checked without a bench.

## Layout

```
bridge/                     Optional Node service: Baileys session, QR, webhooks
agent_x/
  api.py                    Whitelisted endpoints
  core/
    transport/
      __init__.py           Picks the provider named in settings
      base.py               The interface every provider implements
      waclient.py           Hosted gateway  (Frappe Cloud)
      bridge.py             Self-hosted Baileys bridge
    payload.py              Parses WaClient's nested webhook shapes
    webhook.py              Inbound events, authenticated per provider
    messaging.py            Sending and logging
    phone.py                Number normalisation
  agent/
    runtime.py              The agent loop
    provider.py             Gemini, OpenAI, Anthropic
    registry.py             Tool catalogue built from policy
    policy.py               The permission gate
    prompt.py               System prompt
    tools/documents.py      Document tools
  agentx/doctype/           Settings, sessions, messages, runs, actions
tests/                      Stubbed unit tests
```

## Licence

MIT
