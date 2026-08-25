# kyrex-cloud/Dockerfile
# Build context must be the REPO ROOT (kyrex/), not kyrex-cloud/ — this needs
# both kyrex_engine/ and kyrex-cloud/ present as siblings, same layout as a
# local checkout. In Railway: set the Dockerfile path to kyrex-cloud/Dockerfile
# but leave the root/build context as the repo root.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir openai anthropic requests \
    google-api-python-client \
    google-auth-oauthlib \
    google-auth-httplib2

# Scratch root for the fs executor. Container-local and ephemeral: it exists
# so the executor can be exercised end to end, not so anything can be kept
# here. Durable fs work belongs under `kx serve` on a real machine.
RUN mkdir -p /tmp/kyrex-fs \
    && printf 'fs executor scratch space\nWrites here vanish on redeploy.\n' \
       > /tmp/kyrex-fs/README.txt

# The worker is the single execution path: it constructs CloudTaskStore +
# TaskWorker, recovers orphaned tasks on startup, runs the claim loop, and
# (when TELEGRAM_BOT_TOKEN is set) also runs the Telegram poll loop in-process
# so Telegram and the Web API are both submitters to the same task store.
CMD ["python3", "kyrex-cloud/worker.py"]
