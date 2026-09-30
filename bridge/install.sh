#!/usr/bin/env bash
# AgentX WhatsApp bridge installer.
#
# On a fresh server, one line:
#
#   curl -fsSL https://raw.githubusercontent.com/KiruiElisha/agent_x/main/bridge/install.sh | sudo bash
#
# Or from a clone:
#
#   git clone https://github.com/KiruiElisha/agent_x.git
#   cd agent_x/bridge && sudo ./install.sh
#
# Run it again at any time to upgrade. Secrets, settings and the WhatsApp login
# are kept. ./install.sh --help lists the options.

set -euo pipefail

REPO_URL="${AGENTX_REPO:-https://github.com/KiruiElisha/agent_x.git}"
BRANCH="${AGENTX_BRANCH:-main}"
INSTALL_DIR="${AGENTX_DIR:-/opt/agentx}"

MODE=""
DOMAIN=""
SITE_URL=""
WEBHOOK_URL=""
ALLOWED_IPS=""
ASSUME_YES=0
SERVICE="agentx-bridge"
WEBHOOK_PATH="/api/method/agent_x.core.webhook.receive"

usage() {
	cat <<'EOF'
Install or upgrade the AgentX WhatsApp bridge.

Usage: install.sh [options]

  --site URL          Optional fallback site, e.g. https://erp.example.com.
                      Messages for a session that has not registered its own
                      webhook are posted there. Leave it out for a bridge that
                      only serves sites which register themselves.
  --domain NAME       Serve the bridge over HTTPS at this name, with automatic
                      certificates. Use it when this machine does not already
                      host a Frappe site. If a site here has a domain and
                      AgentX installed, leave this out: that site publishes
                      the bridge at https://<site>/agentx-bridge.
  --allow-ip ADDR     Only accept calls from this address or CIDR range; repeat
                      for several. Use your Frappe server's public IP.
  --native            Run under Node and systemd instead of Docker. Suits a
                      server that already runs your Frappe bench.
  --docker            Run in Docker (the default).
  --webhook-url URL   The full webhook URL, if it is not <site>/api/method/...
  --dir PATH          Where to clone when run through curl (default /opt/agentx).
  --branch NAME       Branch to install (default main).
  -y, --yes           Ask nothing; use flags, existing settings and defaults.
  -h, --help          Show this help.

Every option can also come from the environment: BRIDGE_DOMAIN, BRIDGE_SITE,
BRIDGE_WEBHOOK_URL, BRIDGE_ALLOWED_IPS, BRIDGE_MODE, AGENTX_DIR, AGENTX_BRANCH.
EOF
}

# ------------------------------------------------------------------ output

if [ -t 1 ]; then
	BOLD=$'\033[1m' DIM=$'\033[2m' GREEN=$'\033[32m' YELLOW=$'\033[33m' RED=$'\033[31m' RESET=$'\033[0m'
else
	BOLD="" DIM="" GREEN="" YELLOW="" RED="" RESET=""
fi

say() { printf '%s==>%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s!!%s  %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die() {
	printf '%sxx%s  %s\n' "$RED" "$RESET" "$*" >&2
	exit 1
}

# Through `curl | bash` stdin is the script itself, so questions go to the terminal.
interactive() { [ "$ASSUME_YES" -eq 0 ] && [ -r /dev/tty ] && [ -w /dev/tty ]; }

ask() {
	local prompt=$1 default=${2:-} reply=""
	if interactive; then
		if [ -n "$default" ]; then
			printf '%s %s[%s]%s ' "$prompt" "$DIM" "$default" "$RESET" >/dev/tty
		else
			printf '%s ' "$prompt" >/dev/tty
		fi
		IFS= read -r reply </dev/tty || true
	fi
	printf '%s' "${reply:-$default}"
}

confirm() {
	local reply
	interactive || return 0
	printf '%s [Y/n] ' "$1" >/dev/tty
	IFS= read -r reply </dev/tty || true
	case "$reply" in [nN]*) return 1 ;; *) return 0 ;; esac
}

# ----------------------------------------------------------------- options

while [ $# -gt 0 ]; do
	case "$1" in
		--site) SITE_URL=${2:?--site needs a URL}; shift 2 ;;
		--domain) DOMAIN=${2:?--domain needs a name}; shift 2 ;;
		--allow-ip) ALLOWED_IPS="$ALLOWED_IPS ${2:?--allow-ip needs an address}"; shift 2 ;;
		--webhook-url) WEBHOOK_URL=${2:?--webhook-url needs a URL}; shift 2 ;;
		--native) MODE=native; shift ;;
		--docker) MODE=docker; shift ;;
		--dir) INSTALL_DIR=${2:?--dir needs a path}; shift 2 ;;
		--branch) BRANCH=${2:?--branch needs a name}; shift 2 ;;
		-y | --yes) ASSUME_YES=1; shift ;;
		-h | --help) usage; exit 0 ;;
		*) usage >&2; die "Unknown option: $1" ;;
	esac
done

# Flags win over the environment, which wins over what a previous install saved.
DOMAIN=${DOMAIN:-${BRIDGE_DOMAIN:-}}
SITE_URL=${SITE_URL:-${BRIDGE_SITE:-}}
WEBHOOK_URL=${WEBHOOK_URL:-${BRIDGE_WEBHOOK_URL:-}}
ALLOWED_IPS=${ALLOWED_IPS:-${BRIDGE_ALLOWED_IPS:-}}
MODE=${MODE:-${BRIDGE_MODE:-}}

[ "$(id -u)" -eq 0 ] || die "Run as root, e.g.: sudo ./install.sh"
[ "$(uname -s)" = "Linux" ] || die "The installer supports Linux servers only."

# ---------------------------------------------------------------- packages

pkg_install() {
	if command -v apt-get >/dev/null; then
		DEBIAN_FRONTEND=noninteractive apt-get update -qq
		DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null
	elif command -v dnf >/dev/null; then
		dnf install -y -q "$@"
	elif command -v yum >/dev/null; then
		yum install -y -q "$@"
	elif command -v apk >/dev/null; then
		apk add --quiet "$@"
	else
		die "Install $* yourself, then run this again."
	fi
}

need() {
	command -v "$1" >/dev/null && return
	say "Installing $1"
	pkg_install "${2:-$1}"
}

need curl
need openssl

# ------------------------------------------------------------------ source

# Running from a clone uses that clone. Through curl there is no clone yet.
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
	SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
fi

if [ -n "$SCRIPT_DIR" ] && grep -qs '"agentx-whatsapp-bridge"' "$SCRIPT_DIR/package.json"; then
	BRIDGE_DIR=$SCRIPT_DIR
else
	need git
	if [ -d "$INSTALL_DIR/.git" ]; then
		say "Updating $INSTALL_DIR"
		git -c safe.directory='*' -C "$INSTALL_DIR" pull --ff-only --quiet
	else
		say "Downloading AgentX into $INSTALL_DIR"
		git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
	fi
	BRIDGE_DIR="$INSTALL_DIR/bridge"
fi

cd "$BRIDGE_DIR"
ENV_FILE="$BRIDGE_DIR/.env"

env_get() {
	[ -f "$ENV_FILE" ] || return 0
	grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true
}

# ---------------------------------------------------------------- settings

MODE=${MODE:-$(cat .install-mode 2>/dev/null || true)}
MODE=${MODE:-docker}
case "$MODE" in docker | native) ;; *) die "Mode must be docker or native, not $MODE" ;; esac

API_TOKEN=$(env_get BRIDGE_API_TOKEN)
WEBHOOK_SECRET=$(env_get BRIDGE_WEBHOOK_SECRET)
PORT=$(env_get BRIDGE_PORT)
PORT=${PORT:-8787}
LOG_LEVEL=$(env_get BRIDGE_LOG_LEVEL)
SESSION_DIR=$(env_get BRIDGE_SESSION_DIR)
SESSION_DIR=${SESSION_DIR:-./sessions}
AUDIO_MB=$(env_get BRIDGE_INLINE_AUDIO_MAX_MB)
[ -n "$DOMAIN" ] || DOMAIN=$(env_get BRIDGE_DOMAIN)
[ -n "$ALLOWED_IPS" ] || ALLOWED_IPS=$(env_get BRIDGE_ALLOWED_IPS)

if [ -z "$WEBHOOK_URL" ] && [ -n "$SITE_URL" ]; then
	WEBHOOK_URL="${SITE_URL%/}$WEBHOOK_PATH"
fi
[ -n "$WEBHOOK_URL" ] || WEBHOOK_URL=$(env_get BRIDGE_WEBHOOK_URL)

if [ -z "$WEBHOOK_URL" ] && interactive; then
	echo
	echo "${BOLD}Fallback site, optional.${RESET} Each site registers its own webhook when it"
	echo "connects, which is how several tenants share this bridge. Name a site here"
	echo "only if one of them should receive events before it has registered."
	SITE_URL=$(ask "Site URL, or leave empty:")
	[ -n "$SITE_URL" ] && WEBHOOK_URL="${SITE_URL%/}$WEBHOOK_PATH"
fi

if [ "$MODE" = docker ] && [ -z "$DOMAIN" ] && [ ! -f .install-mode ] && interactive; then
	echo
	echo "${BOLD}Is Frappe on this same server?${RESET}"
	echo "If a site here already has a domain and AgentX, leave this empty."
	echo "Other sites, including Frappe Cloud, then use https://<that-site>/agentx-bridge."
	echo "A domain of its own is only needed when this machine does not host Frappe."
	DOMAIN=$(ask "Bridge domain, or leave empty if Frappe runs here:")
fi

if [ -n "$DOMAIN" ] && [ -z "$ALLOWED_IPS" ] && [ ! -f .install-mode ] && interactive; then
	echo
	echo "${BOLD}Who may open the bridge page?${RESET}"
	echo "Leave this empty so clients can sign up in the browser."
	echo "Fill in addresses only if the page should be hidden from everyone else."
	ALLOWED_IPS=$(ask "Allowed IPs, space separated, or empty for everyone:")
fi

# Commas or spaces, either works; the Caddyfile wants spaces.
ALLOWED_IPS=$(printf '%s' "$ALLOWED_IPS" | tr ',' ' ' | xargs || true)
DOMAIN=$(printf '%s' "$DOMAIN" | sed -E 's#^https?://##; s#/.*$##')

case "$WEBHOOK_URL" in
	"" | http://* | https://*) ;;
	*) die "Webhook URL must start with http:// or https://: $WEBHOOK_URL" ;;
esac

if [ -z "$WEBHOOK_URL" ]; then
	warn "No fallback site. Events go only to the webhook each session registers"
	warn "when that site connects or presses Register Webhook."
fi

if [ "$MODE" = native ] && [ -n "$DOMAIN" ]; then
	warn "Native mode does not set up HTTPS. Put the bridge behind your existing web"
	warn "server; see 'Behind nginx' in bridge/README.md. Or re-run with --docker."
fi

gen_secret() { openssl rand -hex 32; }
[ -n "$API_TOKEN" ] || API_TOKEN=$(gen_secret)
[ -n "$WEBHOOK_SECRET" ] || WEBHOOK_SECRET=$(gen_secret)

PROFILES=""
[ "$MODE" = docker ] && [ -n "$DOMAIN" ] && PROFILES="https"

say "Writing $ENV_FILE"
umask 077
PREVIOUS=""
if [ -f "$ENV_FILE" ]; then
	PREVIOUS=$(mktemp)
	cp "$ENV_FILE" "$PREVIOUS"
fi

cat >"$ENV_FILE" <<EOF
# Written by install.sh on $(date -u +%Y-%m-%dT%H:%MZ). Re-running install.sh keeps
# these values. After editing by hand, run: agentx-bridge restart

# Frappe sends this as "Authorization: Bearer <token>". Same as Bridge API Token
# in AgentX Settings.
BRIDGE_API_TOKEN=$API_TOKEN

# Fallback for a session that has not registered its own webhook. Empty is
# normal on a multi-tenant bridge: each site sends its URL when it connects.
# BRIDGE_WEBHOOK_SECRET signs that fallback. A registered site uses its own.
BRIDGE_WEBHOOK_URL=$WEBHOOK_URL
BRIDGE_WEBHOOK_SECRET=$WEBHOOK_SECRET

BRIDGE_HOST=127.0.0.1
BRIDGE_PORT=$PORT
BRIDGE_SESSION_DIR=$SESSION_DIR
BRIDGE_LOG_LEVEL=${LOG_LEVEL:-info}
BRIDGE_INLINE_AUDIO_MAX_MB=${AUDIO_MB:-8}

# HTTPS through Caddy (Docker mode). Empty domain means loopback only.
BRIDGE_DOMAIN=$DOMAIN
BRIDGE_ALLOWED_IPS=$ALLOWED_IPS
COMPOSE_PROFILES=$PROFILES
EOF

# Anything else set by hand in the old file is kept, not silently dropped.
if [ -n "$PREVIOUS" ]; then
	kept=()
	while IFS= read -r line; do
		grep -q "^${line%%=*}=" "$ENV_FILE" || kept+=("$line")
	done < <(grep -E '^[A-Z_][A-Z0-9_]*=' "$PREVIOUS" || true)
	if [ "${#kept[@]}" -gt 0 ]; then
		{
			echo
			echo "# Kept from the previous .env"
			printf '%s\n' "${kept[@]}"
		} >>"$ENV_FILE"
	fi
	rm -f "$PREVIOUS"
fi
umask 022

# ------------------------------------------------------------------ docker

install_docker() {
	if ! command -v docker >/dev/null; then
		confirm "Docker is not installed. Install it now from get.docker.com?" ||
			die "Docker is required. Install it, or re-run with --native."
		say "Installing Docker"
		curl -fsSL https://get.docker.com | sh >/dev/null
		systemctl enable --now docker >/dev/null 2>&1 || true
	fi

	docker compose version >/dev/null 2>&1 ||
		die "Docker Compose v2 is missing. Install the docker-compose-plugin package and re-run."

	say "Building and starting the bridge (the first build takes a few minutes)"
	docker compose up -d --build --remove-orphans
}

# ------------------------------------------------------------------ native

node_ok() {
	command -v node >/dev/null &&
		node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>20||(a===20&&b>=9)?0:1)'
}

install_native() {
	if ! node_ok; then
		command -v apt-get >/dev/null ||
			die "Node.js 20.9 or newer is required. Install it and re-run."
		confirm "Node.js 20.9+ is not installed. Install Node.js 22 from NodeSource?" ||
			die "Node.js is required."
		say "Installing Node.js 22"
		curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
		pkg_install nodejs
	fi
	need git

	# Run as whoever owns the directory, so a clone inside a bench keeps
	# belonging to the frappe user. Only a root-owned clone gets its own user.
	RUN_AS=$(stat -c %U "$BRIDGE_DIR")
	if [ "$RUN_AS" = root ]; then
		RUN_AS=$SERVICE
		id "$RUN_AS" >/dev/null 2>&1 ||
			useradd --system --home-dir "$BRIDGE_DIR" --shell /usr/sbin/nologin "$RUN_AS"
		chown -R "$RUN_AS:" "$BRIDGE_DIR"
	fi
	RUN_HOME=$(getent passwd "$RUN_AS" | cut -d: -f6)

	SESSIONS_PATH=$(cd "$BRIDGE_DIR" && realpath -m "$SESSION_DIR")
	mkdir -p "$SESSIONS_PATH"
	chown "$RUN_AS:" "$SESSIONS_PATH" "$ENV_FILE"
	chmod 700 "$SESSIONS_PATH"

	say "Installing dependencies as $RUN_AS"
	# Baileys pins libsignal to a git+ssh URL; fetch it over HTTPS instead,
	# without touching anybody's git config.
	runuser -u "$RUN_AS" -- env HOME="$RUN_HOME" \
		GIT_CONFIG_COUNT=1 \
		GIT_CONFIG_KEY_0=url.https://github.com/.insteadOf \
		GIT_CONFIG_VALUE_0=ssh://git@github.com/ \
		npm ci --omit=dev --no-audit --no-fund --silent

	say "Installing the $SERVICE service"
	sed -e "s|@DIR@|$BRIDGE_DIR|g" \
		-e "s|@USER@|$RUN_AS|g" \
		-e "s|@NODE@|$(command -v node)|g" \
		deploy/agentx-bridge.service >"/etc/systemd/system/$SERVICE.service"

	systemctl daemon-reload
	systemctl enable "$SERVICE" >/dev/null 2>&1
	systemctl restart "$SERVICE"
}

if [ "$MODE" = docker ]; then
	install_docker
else
	install_native
fi

echo "$MODE" >.install-mode
chmod +x "$BRIDGE_DIR/bridgectl" "$BRIDGE_DIR/install.sh"
ln -sf "$BRIDGE_DIR/bridgectl" /usr/local/bin/agentx-bridge

# Daily update. Re-running the installer keeps tokens and sessions. Set
# BRIDGE_AUTO_UPDATE=0 in the environment before installing to leave this off.
if [ "${BRIDGE_AUTO_UPDATE:-1}" != "0" ]; then
	cp "$BRIDGE_DIR/deploy/agentx-bridge-update.service" /etc/systemd/system/agentx-bridge-update.service
	cp "$BRIDGE_DIR/deploy/agentx-bridge-update.timer" /etc/systemd/system/agentx-bridge-update.timer
	systemctl daemon-reload
	systemctl enable --now agentx-bridge-update.timer >/dev/null 2>&1 || true
fi

# ------------------------------------------------------------------ checks

say "Waiting for the bridge to answer"
healthy=0
for _ in $(seq 1 45); do
	if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
		healthy=1
		break
	fi
	sleep 2
done

if [ "$healthy" -ne 1 ]; then
	warn "The bridge did not come up. Its logs:"
	agentx-bridge logs 50 --no-follow || true
	die "Fix the error above and run the installer again."
fi

BRIDGE_URL="http://127.0.0.1:$PORT"
if [ "$MODE" = docker ] && [ -n "$DOMAIN" ]; then
	BRIDGE_URL="https://$DOMAIN"
	say "Checking HTTPS at $BRIDGE_URL (certificates can take a minute)"
	https_ok=0
	for _ in $(seq 1 30); do
		code=$(curl -s -o /dev/null -w '%{http_code}' "$BRIDGE_URL/health" || true)
		# 403 means Caddy answered with a valid certificate and the allowlist
		# turned this server away, which is what it should do.
		if [ "$code" = 200 ] || [ "$code" = 403 ]; then
			https_ok=1
			break
		fi
		sleep 3
	done
	if [ "$https_ok" -ne 1 ]; then
		warn "HTTPS is not answering yet. Check that $DOMAIN points at this server"
		warn "and that ports 80 and 443 are open, then: agentx-bridge logs caddy"
	fi
fi

# ----------------------------------------------------------------- summary

cat <<EOF

${GREEN}${BOLD}The AgentX bridge is running.${RESET}

In Frappe, open ${BOLD}AgentX Settings → Connection${RESET} and set:

  WhatsApp Provider   Self-Hosted Bridge
  Bridge URL          $BRIDGE_URL
  Bridge API Token    $API_TOKEN
  Webhook Secret      $WEBHOOK_SECRET

Save, press ${BOLD}Test Connection${RESET}, then create a ${BOLD}WhatsApp Session${RESET} and
press Connect to scan the QR code.

EOF

if [ -z "$DOMAIN" ]; then
	echo "${DIM}The Bridge URL above only works from this server."
	echo "Other sites reach it at https://<this-site>/agentx-bridge. Issue a tenant"
	echo "token from AgentX Settings here and paste it into the other site."
	echo "A hostname of its own (re-run with --domain) is only for a machine with no Frappe.${RESET}"
	echo
fi

cat <<EOF
Fallback webhook: ${WEBHOOK_URL:-${YELLOW}(none — each site registers its own)${RESET}}

Manage it with ${BOLD}agentx-bridge${RESET}: status, logs, restart, config, backup, update.
Settings live in $ENV_FILE
EOF
