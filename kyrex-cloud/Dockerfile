# kyrex-cloud/Dockerfile
# Build context must be the REPO ROOT (kyrex/), not kyrex-cloud/ — this needs
# both kyrex_engine/ and kyrex-cloud/ present as siblings, same layout as a
# local checkout. In Railway: set the Dockerfile path to kyrex-cloud/Dockerfile
# but leave the root/build context as the repo root.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir openai anthropic requests

CMD ["python3", "kyrex-cloud/telegram_bot.py"]
