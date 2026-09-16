# Kyrex Browser Host — Phase 1 (local only)

A small, self-hosted browser host for a spare PC. It gives each Bot its own
**persistent Chromium profile** so a login you perform survives across runs,
with CDP bound to **loopback only**.

> **Phase 1 scope.** This directory is the LOCAL HALF of the design. It has no
> Cloud channel, no tunnel, no viewer/takeover route, and no host enrollment.
> Those are Phase 2 and must live in a **separate** file so a Phase-1 machine
> can never reach Cloud by accident.

---

## What is here

| File | Purpose |
|---|---|
| `profiles.py` | per-`(owner, bot_id)` profile directories + opt-in shared profile |
| `Dockerfile` | the host image (system Chromium + Playwright's Python client) |
| `docker-compose.yml` | one persistent Chromium (loopback CDP) + one-shot smoke test |
| `healthcheck.sh` | loopback-only CDP liveness probe (`/json/version`) |
| `smoke_test.py` | harmless static-page + cookie-persistence / isolation check |
| `test_profiles.py` | profile-isolation unit tests |
| `agent.py` | **Phase 2**: outbound authenticated Cloud agent (separate file) |
| `host_allowlist.py` | **Phase 2**: host-side allowlist + CDP/secret redaction |
| `docker-compose.cloud.yml` | **Phase 2**: the agent service (separate stack) |
| `test_agent.py` | **Phase 2**: agent + host-allowlist unit tests |

The operator itself is unchanged in role: `kyrex-cloud/browser_operator.py`
now takes a **managed** mode (below).

---

## The fix that makes persistence real

Previously, a remote session used `connect_over_cdp(...)` then a fresh
`browser.new_context()`. That is a **throwaway incognito profile** — any login
the operator performed vanished on the next run.

`browser_operator.PlaywrightDriver` now honours **managed mode**, selected by
`KYREX_BROWSER_MANAGED=1` (or the mere presence of `KYREX_BROWSER_SESSION_DIR`):

- **managed + local launch** → `launch_persistent_context(user_data_dir=...)`.
  The directory on disk *is* the profile; Chromium reopens it every run.
- **managed + CDP endpoint** → reuse `browser.contexts[0]` (the host's default
  persistent context). No `new_context()`.
- **unmanaged** → unchanged behaviour, so existing callers are unaffected.

A managed CDP guest also **never tears the host's context down** on `close()` —
it detaches, because the persistent profile must outlive the process.

---

## Requirement mapping

1. **Persistent profile per `(owner, bot_id)`** — `profiles.py`; shared-profile
   mode is opt-in (`shared=True`) for your personal account later.
2. **CDP loopback-only** — Chromium is started with
   `--remote-debugging-address=127.0.0.1`; compose publishes **no** port.
3. **Secure connectivity** — **Phase 2, implemented** (see below) as an
   **outbound, authenticated host-agent control channel** (`agent.py` +
   `kyrex-cloud/browser_host_channel.py`). The host dials out, proves
   possession of its enrollment secret with an HMAC, and never opens a
   listener. Raw CDP is never exposed.
4. **Reuse the persistent context** — the managed-mode fix above; covered by
   `kyrex-cloud/test_browser_operator_managed.py`.
5. **No secrets to the model** — cookies/passwords/CDP URLs/session secrets are
   never copied into model text, API responses, logs, or host registration.
6. **Preserve allowlists / isolation / approvals / lifecycle** — the operator's
   allowlist, owner isolation, approval gates and `browser_sessions` records are
   untouched; Phase 1 adds no parallel authority.
7. **Registration / heartbeat / unavailable** — **Phase 2, implemented**:
   enrollment + liveness + `unavailable` state live in
   `kyrex-cloud/browser_hosts.py`; the agent re-registers on every reconnect.
8. **Minimal deployment** — this compose stack; no credentials.

---

## Required safety corrections (revised plan)

These correct the earlier draft and bind Phase 2:

1. **No automatic rerun.** On a host/channel drop, an in-flight browser task is
   marked **interrupted/cancelled** with a clear recovery path. The user must
   **explicitly retry**; nothing is silently re-executed.
2. **Viewer tickets** are short-lived, **single-use**, and bound to
   `owner + bot_id + browser-session`. Raw CDP URLs are **never** exposed.
3. **Cloud is the only policy/approval authority.** Host-side allowlist checks
   are *defense in depth*. The host **rejects any command not received through
   its authenticated Cloud channel**.
4. **Secrets never travel as text.** Provider API keys, passwords, cookies, and
   session secrets are never copied into model text, Chat API responses, logs,
   or host registration.
5. **Phase separation.** The Phase-1 local-only stack is kept separate from
   Phase-2 Cloud connectivity.

---

## Phase 2 — Cloud connectivity (implemented)

The host dials OUT to Cloud over an authenticated websocket and becomes a
worker on that one connection. It is a **separate file** (`agent.py`) launched
by a **separate stack** (`docker-compose.cloud.yml`), so a Phase-1 machine
cannot reach Cloud by accident.

| Piece | Where | Role |
|---|---|---|
| Host agent | `browser-host/agent.py` | outbound WS client; handshake, heartbeat, reconnect, task execution |
| Host allowlist | `browser-host/host_allowlist.py` | independent allowlist + CDP/secret redaction (defense in depth) |
| Cloud registry | `kyrex-cloud/browser_hosts.py` | enrollment, sealed secret, liveness, `unavailable` state, owner/bot binding |
| Cloud channel | `kyrex-cloud/browser_host_channel.py` | frame protocol, policy verdicts, approval pause/resume, task routing |

**Protocol (v1, newline-delimited JSON).** `hello`/`hello_ok` (HMAC-SHA256
proof of the enrollment secret — the secret never travels) → `heartbeat` →
`task` → `operation`/`progress`/`approval`/`result` → Cloud replies `verdict`
(`ALLOW`/`APPROVE`/`DENY`) and `approval_decision`. Cloud is the ONLY
policy/approval authority; the host's allowlist is defense in depth.

**Guarantees.** No public CDP (the agent opens no listener; CDP stays on
loopback). Fail closed when the host is offline/unavailable/stale. Per-`(owner,
bot_id)` profile isolation via `profiles.py`. Enrollment secrets and CDP URLs
are redacted everywhere; the enrollment proof is compared in constant time.

```sh
# Start the Cloud agent (outbound only; no ports published).
KYREX_HOST_ID=box1 KYREX_HOST_OWNER=me \
  KYREX_HOST_CLOUD_URL=wss://cloud.example/browser-host \
  KYREX_HOST_ENROLLMENT_SECRET=... KYREX_HOST_ALLOWLIST=example.com \
  docker compose -f browser-host/docker-compose.cloud.yml --profile cloud up -d agent
```

Enrollment: the Cloud calls `browser_hosts.enroll_host(owner, host_id)`, which
returns the secret **once** (sealed at rest, never returned again); hand that
secret to the host out of band.

### Cloud endpoint (Railway)

The Cloud service is an HTTPS origin on Railway (e.g.
`https://kyrex-production.up.railway.app`). The host dials the **TLS WebSocket
form of the SAME origin**:

```
wss://<railway-host>/api/browser-hosts/ws      # WS_PATH
```

- `POST /api/browser-hosts` (operator-authenticated) enrolls a host and returns
  the secret exactly once; it also returns the exact `wss_url` to configure.
- `GET /api/browser-hosts/endpoint` returns `{"wss_url", "path", "env_var"}` —
  put `wss_url` in the host's `KYREX_HOST_CLOUD_URL`.
- `GET /api/browser-hosts` lists the owner's hosts; `GET
  /api/browser-hosts/{host_id}` shows status; `DELETE` revokes.
- http/ws origins are always coerced **up** to `wss` — a host is never told to
  dial plaintext. There is **no public CDP port** anywhere in this path.

Set `KYREX_PUBLIC_BASE_URL` on Railway to the service's public HTTPS host so the
returned `wss_url` is always correct (falls back to the request origin).

---

## Run it

```sh
# 1. Bring up the persistent browser (loopback CDP, unpublished).
docker compose -f browser-host/docker-compose.yml up -d chromium

# 2. Harmless smoke test: cookie persistence + profile isolation.
docker compose -f browser-host/docker-compose.yml --profile smoke run --rm smoke
```

Profile data lives under `browser-host/profiles/` (git-ignored).

**Do not add a `ports:` mapping to the `chromium` service.** That single line
is what would expose CDP beyond this machine.

---

## Phase 3 — manual viewer (Tailscale-only; implemented in repo, NOT yet activated)

Lets the OWNER take eyes-on + keyboard on ONE Bot's persistent profile
(e.g. logging into Instagram, pairing Google Messages Web), with browser
automation **hard-paused** for that window. Reachability is Tailscale Serve +
tailnet ACLs ONLY. **There is deliberately NO Cloud/Railway viewer route:**
owner keystrokes must never transit Railway, and no other transport can
correctly claim they don't.

| File | Purpose |
|---|---|
| `manual_mode.py` | the durable manual-control boundary: exclusive per-profile `flock` + TTL record; crash/reboot-safe (the kernel drops the lock on death; stale records are reaped automatically) |
| `viewer_ctl.py` | `hold` (in-container lifecycle: lock → Xvfb → headed Chromium → x11vnc → websockify, ALL loopback-only, auto-teardown at TTL) and `start`/`end`/`status`/`reap` (VPS-side control) |
| `Dockerfile.viewer` | the separate viewer image (non-root `viewer` uid 1000; pinned `websockify==0.13.0`, `numpy==1.26.4`; X/VNC/noVNC stack) |
| `docker-compose.viewer.yml` | the **separate** viewer stack: `network_mode: host` (so loopback binds are the host's for `tailscale serve`), NO `ports:` anywhere, non-root, `restart: no`; runs the image ENTRYPOINT (`tini -- viewer_ctl.py hold`) — no empty-command override; declares the shared `KYREX_VIEWER_STATE_DIR` |
| `test_viewer.py` | locking, expiry, crash recovery, agent refusal, isolation, no-published-ports, loopback-only, no-Cloud-egress, pins, offline compose validation |
| `test_viewer_lock.py` | the shared-lock regressions: full-lifetime automation lock, viewer-during-task and task-during-viewer refusal, cross-container shared paths, symlink/traversal rejection, corrected compose command, SIGTERM/SIGKILL recovery, TTL child termination, secret ignore/mode |

**Security shape (asserted by tests):** no `ports:` in any compose file;
viewer Chromium has **zero CDP**; x11vnc+websockify bind `127.0.0.1` only;
Xvfb runs `-nolisten tcp`; a VNC password is REQUIRED (fail closed) and lives
only in a 0600 bind-mounted file; the viewer runs as uid 1000 and refuses
root; no `KYREX_HOST_CLOUD_URL`/secrets ever enter the viewer env; the Cloud
channel gains no viewer/keystroke frame.

**One lock, both sides.** Automation and the manual viewer acquire the SAME
per-`(owner, bot)` kernel `flock` (in `manual_mode.py`) — NOT a check-then-start.
The agent acquires it before it starts the browser operator and **holds it for
the whole task lifetime** (approvals, cancellation, subprocess shutdown,
terminal result); the viewer acquires it before it starts any X/Chromium/VNC
process. If the viewer owns the profile the agent's acquire fails and it returns
`ManualControlActive` before starting anything; if automation owns it the
viewer's acquire fails and it refuses. Both TOCTOU directions are closed, so two
Chromium processes can never share a profile. The lock + records live under the
shared profiles volume at `<profiles_root>/.kyrex-viewer-state` — the SAME path
in the agent and viewer containers (`KYREX_VIEWER_STATE_DIR`), so a `/run` split
cannot hide one side from the other. A live manual session on ANY profile is an
ADDITIONAL best-effort host-wide refusal. Two independent TTL enforcement points
(record expiry + the viewer's own watchdog, which SIGTERM/SIGKILLs the
X/Chromium/VNC process groups) mean a hung websockify can never keep a host
"manual".

### Not yet done — VPS activation (deliberately out of scope here)

1. `docker compose -f browser-host/docker-compose.viewer.yml build viewer`.
   The base image is already DIGEST-PINNED in `Dockerfile.viewer`
   (`python:3.11-slim@sha256:9534e5a8…`) and the pip deps are exact; re-verify
   only when intentionally bumping: `docker buildx imagetools inspect python:3.11-slim`.
2. `chown -R 1000:1000` the target profile subtree under `browser-host/profiles/`
   so uid-1000 Chromium can open it; Tailscale installed + `tailscale up`
   (OWNER's tailnet only); tailnet ACL that scopes the serve target to the
   owner identity; `tailscale serve --https=443 http://127.0.0.1:6080`.
   Never `tailscale funnel` — that would make the viewer public.
3. Create the VNC password file (mode 0600; `browser-host/viewer-vnc-pass` is
   git-ignored, and the viewer REFUSES a file that is not 0600):
   `umask 077; pwgen 16 1 > browser-host/viewer-vnc-pass; chmod 600 browser-host/viewer-vnc-pass`
   then `KYREX_VIEWER_VNC_PASSWORD_FILE=$PWD/browser-host/viewer-vnc-pass`.
   Also pre-create the SHARED state dir writable by uid 1000 so both containers
   agree on it: `install -d -m 700 -o 1000 -g 1000 browser-host/profiles/.kyrex-viewer-state`.
4. Start + verify per session:
   `python3 browser-host/viewer_ctl.py start --owner <owner> --bot <bot> --ttl 2700`
   → owner's phone opens `https:<vps-tailnet-name>/#/` (noVNC in-browser,
   Tailscale client needed on the phone) → automation eligibility returns
   automatically at TTL or `viewer_ctl.py end`.
5. `docker compose config -q` for each stack (needs docker; the offline
   semantic checks in `test_viewer.py` cover the same invariants meanwhile).
