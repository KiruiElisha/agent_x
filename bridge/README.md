# AgentX WhatsApp Bridge

A small Node service that owns the WhatsApp Web session. Frappe never speaks the
WhatsApp protocol itself: it asks this bridge for a QR code, and the bridge posts
inbound messages back to Frappe as signed webhooks.

## Why a separate process

Baileys keeps a long-lived socket and an encrypted session on disk. Frappe's
workers are short-lived and forked, so the socket cannot live inside them.

## Install

Pick the line that matches where Frappe runs. Each installs the bridge, generates
its secrets, starts it, and prints the three values to paste into **AgentX
Settings → Connection**.

**Frappe on another server** (Frappe Cloud, or a separate box). Point a DNS
name at this server first, and open ports 80 and 443:

```bash
curl -fsSL https://raw.githubusercontent.com/KiruiElisha/agent_x/main/bridge/install.sh \
  | sudo bash -s -- --site https://erp.example.com --domain bridge.example.com
```

This runs the bridge in Docker behind Caddy, which gets and renews the HTTPS
certificate by itself. Add `--allow-ip <Frappe server IP>` so nothing else can
reach the bridge. Frappe Cloud shows the server's IP on the site dashboard.

**Frappe on this same server.** Nothing needs to be public:

```bash
curl -fsSL https://raw.githubusercontent.com/KiruiElisha/agent_x/main/bridge/install.sh \
  | sudo bash -s -- --site https://erp.example.com
```

The Bridge URL is then `http://127.0.0.1:8787`. On a bench server you may prefer
`--native`, which runs it with Node under systemd instead of Docker.

**From a clone**, run the same thing without curl. With no options it asks for
what it needs:

```bash
git clone https://github.com/KiruiElisha/agent_x.git
cd agent_x/bridge
sudo ./install.sh
```

Run the installer again at any time to upgrade. Secrets, settings and the
WhatsApp login are kept. `./install.sh --help` lists every option.

What it needs: a Linux server (Ubuntu or Debian are tested paths), root, and
1 GB of RAM. Docker is installed for you if it is missing; `--native` installs
Node.js 22 instead.

## Day to day

The installer adds an `agentx-bridge` command:

| Command | What it does |
| --- | --- |
| `agentx-bridge status` | Running or not, and which WhatsApp sessions are linked |
| `agentx-bridge logs` | Follow the logs (`logs caddy` for the HTTPS proxy) |
| `agentx-bridge config` | Print the values for AgentX Settings again |
| `agentx-bridge restart` | Restart, picking up edits to `.env` |
| `agentx-bridge backup` | Save the WhatsApp login to a `.tgz` |
| `agentx-bridge restore FILE` | Put a backup back, e.g. on a new server |
| `agentx-bridge update` | Pull the latest code and reinstall |

Settings live in `bridge/.env`. To change the domain or the Frappe site, edit
it and run `agentx-bridge restart`, or re-run the installer with the new flags.

Back the login up. Losing it means scanning the QR code again. A backup file
*is* the login: whoever holds it can use the number.

## Run it by hand

For development, or a setup the installer does not cover:

```bash
cd bridge
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=url.https://github.com/.insteadOf \
  GIT_CONFIG_VALUE_0=ssh://git@github.com/ npm ci
cp .env.example .env
# fill in BRIDGE_API_TOKEN, BRIDGE_WEBHOOK_URL, BRIDGE_WEBHOOK_SECRET
npm start
```

The `GIT_CONFIG_*` variables are there because Baileys pins one dependency to a
`git+ssh://github.com` URL, which fails on a machine without a GitHub SSH key.
They fetch it over HTTPS for this one command, without changing your git config.

`npm start` reads `.env`. The bridge refuses to start without
`BRIDGE_API_TOKEN` or `BRIDGE_WEBHOOK_SECRET`: Frappe rejects unsigned events,
so a bridge without a secret would drop every message.

Keep `BRIDGE_HOST` on `127.0.0.1`. Anyone who can reach the port and has the
token can send from the linked number.

### Under supervisor

Bench already runs supervisor, so the bridge can sit alongside the other
processes:

```ini
[program:agentx-bridge]
command=node --env-file=.env src/index.js
directory=/home/frappe/frappe-bench/apps/agent_x/bridge
user=frappe
autostart=true
autorestart=true
environment=NODE_ENV="production"
stdout_logfile=/home/frappe/frappe-bench/logs/agentx-bridge.log
stderr_logfile=/home/frappe/frappe-bench/logs/agentx-bridge.error.log
```

### Behind nginx

A native install listens on `127.0.0.1:8787` only. If Frappe is on another
server, publish it through the nginx you already run:

```nginx
server {
    listen 443 ssl;
    server_name bridge.example.com;
    # ssl_certificate lines as for your other sites

    allow 203.0.113.10;   # your Frappe server
    deny all;

    client_max_body_size 30m;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_read_timeout 60s;
    }
}
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
