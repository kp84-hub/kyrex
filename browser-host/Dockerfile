# browser-host/Dockerfile - PHASE 1 (LOCAL ONLY).
#
# Build context MUST be the repo root (kyrex/), because this image runs the
# real Browser Operator (kyrex-cloud/browser_operator.py) against a LOCAL
# persistent profile. There is NO Cloud channel, tunnel, viewer, or enrollment
# here - that is Phase 2 and lives elsewhere.
#
#   docker compose -f browser-host/docker-compose.yml build
#
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    curl \
    tini \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Playwright's PYTHON CLIENT only. The browser binary is the system chromium
# above (KYREX_BROWSER_EXECUTABLE), so there is no `playwright install`
# download and nothing is fetched at runtime.
RUN pip install --no-cache-dir playwright

COPY browser-host/healthcheck.sh /usr/local/bin/healthcheck.sh
RUN chmod 0755 /usr/local/bin/healthcheck.sh

WORKDIR /host
COPY kyrex-cloud/browser_operator.py /host/browser_operator.py
COPY browser-host/profiles.py /host/profiles.py
COPY browser-host/smoke_test.py /host/smoke_test.py

ENV KYREX_BROWSER_DRIVER=playwright \
    KYREX_BROWSER_EXECUTABLE=/usr/bin/chromium \
    KYREX_BROWSER_MANAGED=1 \
    KYREX_BROWSER_PROFILES_ROOT=/profiles

ENTRYPOINT ["tini", "--"]
CMD ["/usr/bin/chromium", \
     "--remote-debugging-port=9222", \
     "--remote-debugging-address=127.0.0.1", \
     "--user-data-dir=/profiles/bot-b1/owner-me", \
     "--no-first-run", \
     "--no-default-browser-check", \
     "--headless=new", \
     "--disable-dev-shm-usage"]
