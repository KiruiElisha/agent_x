# AgentX WhatsApp bridge.
#
#   docker compose up -d        (see docker-compose.yml, or run ./install.sh)

FROM node:22-bookworm-slim AS deps

WORKDIR /app

# Baileys pins libsignal to a git+ssh URL, which fails on a machine with no
# GitHub SSH key. Fetch it over HTTPS instead, for this install only.
RUN apt-get update \
	&& apt-get install -y --no-install-recommends git ca-certificates \
	&& rm -rf /var/lib/apt/lists/*

COPY package.json package-lock.json ./
RUN GIT_CONFIG_COUNT=1 \
	GIT_CONFIG_KEY_0=url.https://github.com/.insteadOf \
	GIT_CONFIG_VALUE_0=ssh://git@github.com/ \
	npm ci --omit=dev --no-audit --no-fund


FROM node:22-bookworm-slim

ENV NODE_ENV=production \
	BRIDGE_HOST=0.0.0.0 \
	BRIDGE_PORT=8787 \
	BRIDGE_SESSION_DIR=/data/sessions \
	BRIDGE_IN_CONTAINER=1

WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY package.json ./
COPY src ./src

# WhatsApp credentials live here. A named volume copies this ownership, so the
# unprivileged user can write to it.
RUN mkdir -p /data/sessions && chown -R node:node /data
VOLUME /data

USER node
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
	CMD node -e "fetch('http://127.0.0.1:8787/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "src/index.js"]
