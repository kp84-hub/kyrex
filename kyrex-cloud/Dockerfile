# kyrex-cloud/Dockerfile
# Build context must be the REPO ROOT (kyrex/), not kyrex-cloud/ — this needs
# both kyrex_engine/ and kyrex-cloud/ present as siblings, same layout as a
# local checkout. In Railway: set the Dockerfile path to kyrex-cloud/Dockerfile
# but leave the root/build context as the repo root.
FROM node:20-slim AS chatui
WORKDIR /chatui
COPY kyrex-chat/package.json kyrex-chat/package-lock.json /chatui/
RUN npm ci --no-audit --no-fund
COPY kyrex-chat/ /chatui/
RUN npm run build

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates tzdata bubblewrap tini chromium fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
COPY --from=chatui /chatui/dist /app/kyrex-chat/dist

# `websockets` is REQUIRED (declared explicitly, never ambient/transitive):
# uvicorn only performs the RFC 6455 upgrade when a websocket implementation is
# importable. Without it uvicorn hands the Browser Host upgrade to FastAPI as a
# plain HTTP GET, and the session-guarded GET /api/browser-hosts/{host_id}
# route answers 401 — misreporting a transport fault as an auth failure.
RUN pip install --no-cache-dir ./kyrex_engine fastapi uvicorn websockets openai anthropic requests cryptography playwright \
    google-api-python-client \
    google-auth-oauthlib \
    google-auth-httplib2 \
    google-cloud-firestore

# Scratch root for the fs executor. Container-local and ephemeral: it exists
# so the executor can be exercised end to end, not so anything can be kept
# here. Durable fs work belongs under `kx serve` on a real machine.
RUN mkdir -p /tmp/kyrex-fs \
    && printf 'fs executor scratch space\nWrites here vanish on redeploy.\n' \
       > /tmp/kyrex-fs/README.txt

CMD ["tini", "--", "python3", "kyrex-cloud/web/entrypoint.py"]
