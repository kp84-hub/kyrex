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

# tesseract-ocr (+ its English traineddata) is the REAL local OCR engine for
# the fixed-purpose Level 6 weekly operation: browser-host/level6_post.py
# shells out to it on a POST-ELEMENT screenshot and returns only the OCR text,
# never image bytes. Pinned to the distro packages; nothing is fetched at
# runtime and no OCR model is downloaded.
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    xvfb \
    curl \
    tini \
    fonts-liberation \
    imagemagick \
    tesseract-ocr \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Playwright's PYTHON CLIENT only. The browser binary is the system chromium
# above (KYREX_BROWSER_EXECUTABLE), so there is no `playwright install`
# download and nothing is fetched at runtime. `websockets` is the OUTBOUND
# Cloud transport used by agent.py (Phase 2) - it is a client only; no
# listening socket is ever opened, and CDP stays on loopback.
RUN pip install --no-cache-dir playwright websockets

COPY browser-host/healthcheck.sh /usr/local/bin/healthcheck.sh
RUN chmod 0755 /usr/local/bin/healthcheck.sh

WORKDIR /host
COPY kyrex-cloud/browser_operator.py /host/browser_operator.py
# The fixed-purpose Level 6 weekly host operation. browser_operator.py imports
# it lazily (only when a `level6_weekly` task arrives), so it must sit in the
# SAME import directory as the operator.
COPY browser-host/level6_post.py /host/level6_post.py
COPY browser-host/level6_photos_scanner.py /host/level6_photos_scanner.py
COPY browser-host/profiles.py /host/profiles.py
COPY browser-host/smoke_test.py /host/smoke_test.py
# Phase 2 (Cloud connectivity). Copied, NOT wired into the default CMD: a
# Phase-1 machine never reaches Cloud unless it is explicitly started.
COPY browser-host/agent.py /host/agent.py
COPY browser-host/messages_watcher.py /host/messages_watcher.py
COPY browser-host/host_allowlist.py /host/host_allowlist.py
# The manual-viewer control boundary: agent.py does `import manual_mode`
# inside /host, so it must ship in the SAME import directory there.
COPY browser-host/manual_mode.py /host/manual_mode.py

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
