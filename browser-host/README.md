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
3. **Secure connectivity** — *out of scope for Phase 1*. Recommended direction
   (Phase 2): an **outbound, authenticated host-agent control channel** for a
   personal v1, hardened with Tailscale for the product-grade fleet. Raw CDP is
   never exposed.
4. **Reuse the persistent context** — the managed-mode fix above; covered by
   `kyrex-cloud/test_browser_operator_managed.py`.
5. **No secrets to the model** — cookies/passwords/CDP URLs/session secrets are
   never copied into model text, API responses, logs, or host registration.
6. **Preserve allowlists / isolation / approvals / lifecycle** — the operator's
   allowlist, owner isolation, approval gates and `browser_sessions` records are
   untouched; Phase 1 adds no parallel authority.
7. **Registration / heartbeat / unavailable** — *Phase 2*.
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
