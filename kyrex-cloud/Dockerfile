# kyrex-cloud/Dockerfile
# Build context must be the REPO ROOT (kyrex/), not kyrex-cloud/ — this needs
# both kyrex_engine/ and kyrex-cloud/ present as siblings, same layout as a
# local checkout. In Railway: set the Dockerfile path to kyrex-cloud/Dockerfile
# but leave the root/build context as the repo root.
FROM emscripten/emsdk:4.0.23 AS voicewake
WORKDIR /voice-build
COPY kyrex-voice/build_web_wake.py /voice-build/kyrex-voice/build_web_wake.py
COPY kyrex-voice-android/app/src/main/assets/licenses /voice-build/kyrex-voice-android/app/src/main/assets/licenses
RUN python3 kyrex-voice/build_web_wake.py

FROM node:22-bookworm-slim AS developer-node
FROM golang:1.26-bookworm AS developer-go

FROM developer-node AS chatui
WORKDIR /chatui
COPY kyrex-chat/package.json kyrex-chat/package-lock.json /chatui/
RUN npm ci --no-audit --no-fund
COPY kyrex-chat/ /chatui/
COPY --from=voicewake /voice-build/kyrex-chat/public/voice-wake /chatui/public/voice-wake
RUN npm run build

FROM python:3.11-slim AS developer-runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates tzdata bubblewrap tini chromium fonts-liberation build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Developer Bot runs in this final image, not in the frontend build stage.
COPY --from=developer-node /usr/local/bin/node /usr/local/bin/node
COPY --from=developer-node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=developer-go /usr/local/go /usr/local/go
ENV PATH="/usr/local/go/bin:${PATH}"
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && pip install --no-cache-dir 'pytest>=8,<10' 'pytest-asyncio>=0.23,<2' 'pytest-mock>=3.14,<4' pre-commit \
    && python -m pytest --version && node --version && npm --version && go version

FROM developer-runtime AS app
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
    'google-auth>=2.40,<3' google-auth-oauthlib \
    google-auth-httplib2 \
    google-cloud-firestore pywebpush==2.1.2

# Scratch root for the fs executor. Container-local and ephemeral: it exists
# so the executor can be exercised end to end, not so anything can be kept
# here. Durable fs work belongs under `kx serve` on a real machine.
RUN mkdir -p /tmp/kyrex-fs \
    && printf 'fs executor scratch space\nWrites here vanish on redeploy.\n' \
       > /tmp/kyrex-fs/README.txt

CMD ["tini", "--", "python3", "kyrex-cloud/web/entrypoint.py"]
