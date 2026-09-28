# AgentX WhatsApp Bridge

A small Node service that owns the WhatsApp Web session. Frappe never speaks the
WhatsApp protocol itself: it asks this bridge for a QR code, and the bridge posts
inbound messages back to Frappe as signed webhooks.

## Why a separate process

Baileys keeps a long-lived socket and an encrypted session on disk. Frappe's
workers are short-lived and forked, so the socket cannot live inside them.

## Run it

```bash
cd bridge
npm install
cp .env.example .env
# fill in BRIDGE_API_TOKEN, BRIDGE_WEBHOOK_URL, BRIDGE_WEBHOOK_SECRET
npm start
```

The same values go into **AgentX Settings** in Frappe. The bridge refuses to
start without `BRIDGE_API_TOKEN` or `BRIDGE_WEBHOOK_SECRET`: Frappe rejects
unsigned events, so a bridge without a secret would drop every message.

Keep `BRIDGE_HOST` on `127.0.0.1`. Anyone who can reach the port and has the
token can send from the linked number. Back up `BRIDGE_SESSION_DIR`; losing it
means scanning the QR again.

## Under supervisor

Bench already runs supervisor, so add the bridge alongside the other processes:

```ini
[program:agentx-bridge]
command=node src/index.js
directory=/home/frappe/frappe-bench/apps/agent_x/bridge
user=frappe
autostart=true
autorestart=true
environment=NODE_ENV="production"
stdout_logfile=/home/frappe/frappe-bench/logs/agentx-bridge.log
stderr_logfile=/home/frappe/frappe-bench/logs/agentx-bridge.error.log
```

## API

Every route needs `Authorization: Bearer $BRIDGE_API_TOKEN`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness, no auth |
| GET | `/api/sessions` | Status of every live session |
| POST | `/api/sessions/:id/start` | Begin pairing or resume |
| GET | `/api/sessions/:id/qr` | Current QR as a data URL |
| GET | `/api/sessions/:id/status` | One session's state |
| POST | `/api/sessions/:id/send` | `{to, text}` or `{to, media}` |
| POST | `/api/sessions/:id/check` | Is this number on WhatsApp |
| GET | `/api/sessions/:id/catalog` | Business catalog (`?jid=&limit=&cursor=`, defaults to own account) |
| GET | `/api/sessions/:id/collections` | Catalog collections (`?jid=&limit=`) |
| POST | `/api/sessions/:id/products` | Add a catalog product |
| PATCH | `/api/sessions/:id/products/:productId` | Edit a catalog product |
| DELETE | `/api/sessions/:id/products` | `{productIds: [...]}`, bulk delete |
| GET | `/api/sessions/:id/orders/:orderId` | Order details (`?token=` from the order message) |
| POST | `/api/sessions/:id/stop` | Close the socket, keep credentials |
| POST | `/api/sessions/:id/logout` | Unlink and forget credentials |
| DELETE | `/api/sessions/:id` | Logout and drop the session |

## Events posted to Frappe

`qr`, `qr_expired`, `connected`, `disconnected`, `logged_out`, `message`, `receipt`.

Each body carries `event`, `session`, and `at`, and is signed with
`X-AgentX-Signature: sha256=<hmac of the raw body>`.
