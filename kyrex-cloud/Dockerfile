# kyrex-cloud/Dockerfile
# Build context must be the REPO ROOT (kyrex/), not kyrex-cloud/ — this needs
# both kyrex_engine/ and kyrex-cloud/ present as siblings, same layout as a
# local checkout. In Railway: set the Dockerfile path to kyrex-cloud/Dockerfile
# but leave the root/build context as the repo root.
FROM python:3.11-slim

# bubblewrap is REQUIRED: the engine refuses to run model-driven commands
# unsandboxed in cloud mode (KYREX_SURFACE=cloud), because the worker
# environment carries bot tokens, API keys and repo credentials. Without
# bwrap the container would either fail every run_command or — pre-fix —
# hand them the full secret environment unsandboxed (security review, HIGH).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates tzdata bubblewrap \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# fastapi + uvicorn are pre-installed because the sandboxed engine has no
# network (--unshare-all): test runs inside tasks cannot pip install them.
RUN pip install --no-cache-dir openai anthropic requests \
    google-api-python-client \
    google-auth-oauthlib \
    google-auth-httplib2 \
    fastapi uvicorn

# Scratch root for the fs executor. Container-local and ephemeral: it exists
# so the executor can be exercised end to end, not so anything can be kept
# here. Durable fs work belongs under `kx serve` on a real machine.
RUN mkdir -p /tmp/kyrex-fs \
    && printf 'fs executor scratch space\nWrites here vanish on redeploy.\n' \
       > /tmp/kyrex-fs/README.txt

CMD ["python3", "kyrex-cloud/telegram_bot.py"]
