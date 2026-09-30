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

**Frappe on this same server.** Nothing needs to be public, and you do not need a
second domain. The installer keeps the bridge on loopback:

```bash
curl -fsSL https://raw.githubusercontent.com/KiruiElisha/agent_x/main/bridge/install.sh \
  | sudo bash -s -- --site https://erp.example.com
```

The Bridge URL is then `http://127.0.0.1:8787`. On a bench server you may prefer
`--native`, which runs it with Node under systemd instead of Docker. Other
ERPNext sites, including Frappe Cloud, call this same bridge through a site
that already has AgentX and a domain. See
[Several sites](#several-sites-including-erpnext-on-another-server).

**The bridge on its own server**, with no Frappe site on it. Point a DNS name
at this server first, and open ports 80 and 443:

```bash
curl -fsSL https://raw.githubusercontent.com/KiruiElisha/agent_x/main/bridge/install.sh \
  | sudo bash -s -- --site https://erp.example.com --domain bridge.example.com
```

This runs the bridge in Docker behind Caddy, which gets and renews the HTTPS
certificate by itself. Add `--allow-ip <Frappe server IP>` so nothing else can
reach the bridge. Frappe Cloud shows the server's IP on the site dashboard.
`--site` is the fallback webhook for that one site. Each additional site
registers its own URL when it connects, so this flag does not have to name
every tenant.

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

## Clean reinstall

Use this when the bridge answers `Cannot GET /`, `agentx-bridge update` fails,
or the checkout and containers no longer match. It removes the installed copy
and the stored WhatsApp logins. Clients sign up again afterwards, and each
number has to be linked again.

If you still want the current logins, run `sudo agentx-bridge backup` first and
restore it after the new install. Skip the backup if the data itself is what
is corrupt.

Stop whichever process is running. A Docker install lives in `/opt/agentx`
unless you passed `--dir`. A native install is the systemd service of the same
name.

```bash
# Docker (the default installer)
cd /opt/agentx/bridge 2>/dev/null && sudo docker compose --profile https down -v --remove-orphans

# Native, or a leftover service next to Docker
sudo systemctl disable --now agentx-bridge 2>/dev/null || true
sudo rm -f /etc/systemd/system/agentx-bridge.service
sudo systemctl daemon-reload

# Bench supervisor, if the bridge was added there by hand
sudo supervisorctl stop agentx-bridge 2>/dev/null || true
```

Remove the checkout, the command, and any Compose volumes the `down` did not
catch:

```bash
sudo rm -rf /opt/agentx
sudo rm -f /usr/local/bin/agentx-bridge
sudo docker volume ls -q | grep '^agentx-bridge_' | xargs -r sudo docker volume rm
```

Install it again. Leave `--site` out. Clients register from the page, or from
**Sign up this site** on their ERPNext. Pass `--domain` only when this machine
does not already host Frappe:

```bash
curl -fsSL https://raw.githubusercontent.com/KiruiElisha/agent_x/main/bridge/install.sh \
  | sudo bash -s -- --yes --domain whatsapp.site.com
```

On a bench that already has a domain, omit `--domain` and add `--native` if you
do not want Docker. Open `https://whatsapp.site.com` (or
`https://<this-site>/agentx-bridge`) and confirm the signup page is there, not
`Cannot GET /`.

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
`BRIDGE_WEBHOOK_SECRET` signs the fallback webhook. A site that registers its
own secret is signed with that instead.

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

A native install listens on `127.0.0.1:8787` only. When a Frappe site on this
same machine already has a domain, you do not need the block below: that site
serves `/agentx-bridge` and forwards it to this port. Use nginx only when the
bridge has to answer on a name of its own:

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

Every route except `/health` needs `Authorization: Bearer`. Use `BRIDGE_API_TOKEN`
for this server, or a tenant token for another site.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness, no auth |
| GET | `/api/sessions` | Status of every live session |
| POST | `/api/sessions/:id/start` | Begin pairing or resume. Body may set `webhook_url` and `webhook_secret` for this session |
| POST | `/api/sessions/:id/webhook` | Store this session's webhook without opening the socket |
| POST | `/api/sessions/:id/pair` | `{phone}` — 8 character pairing code |
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
| POST | `/api/tenants` | Master token only. `{id}` returns a token for one client, once |
| GET | `/api/tenants` | Master token only. Ids, not the tokens |
| DELETE | `/api/tenants/:id` | Master token only. Forget that token |

The master token (`BRIDGE_API_TOKEN`) can use every session. A tenant token can
only use the sessions it has started. Two clients who both name a session `main`
do not share a WhatsApp link.

## Console

Opening the bridge in a browser is the client page, the same idea as WaClient.
`https://whatsapp.site.com` shows the signup form. A visitor enters a client id
and an email address. The page shows the token, and if SMTP is set the same
details are emailed. Nothing about their Frappe site is asked at install time.

On Frappe Cloud, set **Bridge URL** to that hostname, choose Self-Hosted Bridge,
and press **Sign up this site**. The token is saved on that site, the webhook
is registered, and the email goes to the user who pressed the button. Then
open a WhatsApp Session and press Connect.

The master token is only for the operator view on the same page.

On this server the page is `http://127.0.0.1:8787`. From anywhere else it is
the public hostname, or:

```text
https://<this-site>/agentx-bridge
```

Set `BRIDGE_SIGNUP=0` in the bridge environment to turn public signup off.
`BRIDGE_MAX_TENANTS` (default 200) is how many clients the page will create.

## Several sites, including ERPNext on another server

The bridge stays on `127.0.0.1`. Any site on this bench where AgentX is installed
publishes the same console and API at:

```text
https://<that-site>/agentx-bridge
```

No extra domain. You can issue the token from the console, or from **Issue
Tenant Token** in AgentX Settings on this server. A cloud ERPNext site sets:

- WhatsApp Provider: Self-Hosted Bridge
- Bridge URL: `https://<that-site>/agentx-bridge`
- Bridge API Token: the token from **Issue Tenant Token** on this server
- Webhook Secret: any secret you generate there
- Public Base URL: the cloud site's own HTTPS address, if it is not detected

Connect, or Register Webhook, stores that cloud site's webhook on its session.
Events for that session are posted there and signed with that site's secret.
Message times and business hours use the time zone in System Settings on the
site that receives the event.

On this server, leave Bridge URL as `http://127.0.0.1:8787` and keep the master
token. That site does not go through `/agentx-bridge`.

## Events posted to Frappe

`qr`, `qr_expired`, `pairing_code`, `connected`, `disconnected`, `logged_out`,
`message`, `receipt`.

Each body carries `event`, `session`, and `at`. `at` and message timestamps are
UTC. Frappe stores them in the time zone from that site's System Settings.
The body is signed with `X-AgentX-Signature: sha256=<hmac of the raw body>`,
using that session's webhook secret, or `BRIDGE_WEBHOOK_SECRET` when the
session has not registered one.

Pairing works two ways. Connect returns a QR and Desk keeps asking for a fresh
one until it is scanned or the phone links. `POST /api/sessions/:id/pair` with
`{"phone": "2547..."}` returns an eight-character code, shown as `ABCD-EFGH`,
which is typed under Linked Devices → Link with phone number.
