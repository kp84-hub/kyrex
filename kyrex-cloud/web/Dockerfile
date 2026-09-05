# kyrex-cloud/web/Dockerfile
# Build context must be the REPO ROOT (kyrex/), not kyrex-cloud/web/ — this needs
# both kyrex_engine/ and kyrex-cloud/ present as siblings, same layout as a
# local checkout.  On Render: set the root directory to the repo root and the
# Dockerfile path to kyrex-cloud/web/Dockerfile.

# Stage 1: build the Chat SPA (kyrex-chat). Its dist output is gitignored, so
# it must be produced inside the image for the web app to serve it on the
# Chat product host (KYREX_CHAT_PUBLIC_HOST, e.g. chat.kyrex.dev).
FROM node:20-slim AS chatui
WORKDIR /chatui
COPY kyrex-chat/package.json kyrex-chat/package-lock.json /chatui/
RUN npm ci --no-audit --no-fund
COPY kyrex-chat/ /chatui/
RUN npm run build

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates tzdata bubblewrap tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
COPY --from=chatui /chatui/dist /app/kyrex-chat/dist

RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    openai \
    anthropic \
    requests \
    google-api-python-client \
    google-auth-oauthlib \
    google-auth-httplib2

# Scratch root for the filesystem executor. It is container-local and
# ephemeral; durable state remains under KYREX_DATA_DIR.
RUN mkdir -p /tmp/kyrex-fs \
    && printf 'fs executor scratch space\nWrites here vanish on redeploy.\n' \
       > /tmp/kyrex-fs/README.txt

EXPOSE 8000

CMD ["tini", "--", "python3", "kyrex-cloud/web/entrypoint.py"]