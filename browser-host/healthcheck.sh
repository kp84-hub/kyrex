#!/bin/sh
# browser-host/healthcheck.sh - loopback-only CDP liveness probe.
#
# Chromium is started with --remote-debugging-address=127.0.0.1, so this probe
# must only ever contact 127.0.0.1 - never a routable address. A passing check
# means the DevTools endpoint answered on loopback.
set -eu

PORT="${KYREX_CDP_PORT:-9222}"

exec curl -fsS "http://127.0.0.1:${PORT}/json/version" >/dev/null
