# kyrex-cloud/web/Dockerfile
# Build context must be the REPO ROOT (kyrex/), not kyrex-cloud/web/ — this needs
# both kyrex_engine/ and kyrex-cloud/ present as siblings, same layout as a
# local checkout.  On Render: set the root directory to the repo root and the
# Dockerfile path to kyrex-cloud/web/Dockerfile.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir fastapi uvicorn openai anthropic requests

EXPOSE 8000

CMD ["python3", "kyrex-cloud/web/backend/main.py"]