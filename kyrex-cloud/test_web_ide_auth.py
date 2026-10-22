#!/usr/bin/env python3
"""Tests for the IDE (desktop) client auth plumbing in web/backend.

The Kyrex IDE is a Tauri desktop app: a different origin from the web
frontend, with no shared cookie jar. Three pieces make it a first-class
client:

  1. `?client=ide` OAuth state marking — /auth/callback returns a token
     page instead of the cookie redirect, so the operator can paste the
     session token into the IDE.
  2. Header session tokens (X-Session-Token / Bearer) accepted everywhere
     the cookie is accepted.
  3. A CORS allowlist for the desktop origin(s) via WEB_CORS_ORIGINS.

The pure logic lives in web/backend/ide_auth.py (stdlib-only, so this
harness runs without FastAPI installed). main.py cannot be imported here,
so its wiring is checked by compiling the module and asserting each
integration point exists.

Run: python3 test_web_ide_auth.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "web", "backend"))

import ide_auth  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# ── 1. IDE OAuth state marking ─────────────────────────────────────
print("\nTest 1: ide state — IDE flows are marked, browser states are not")

state = ide_auth.make_ide_state()
check("ide state carries the ide: prefix",
      state.startswith(ide_auth.IDE_STATE_PREFIX), f"state={state}")
check("ide state has secret material after the prefix",
      len(state) > len(ide_auth.IDE_STATE_PREFIX))
check("ide state is unique per call",
      ide_auth.make_ide_state() != state)
check("is_ide_state accepts its own state",
      ide_auth.is_ide_state(state))
check("is_ide_state rejects None",
      not ide_auth.is_ide_state(None))
check("is_ide_state rejects a browser state",
      not ide_auth.is_ide_state("9f1c2e4a5b6d7e8f"))
check("is_ide_state rejects the empty string",
      not ide_auth.is_ide_state(""))


# ── 2. Header session tokens ───────────────────────────────────────
print("\nTest 2: session_token_from — header credentials win, cookie fallback")

check("X-Session-Token honoured",
      ide_auth.session_token_from(None, {"X-Session-Token": "tok-1"}) == "tok-1")
check("X-Session-Token beats the cookie",
      ide_auth.session_token_from("cookie", {"X-Session-Token": "tok-1"}) == "tok-1")
check("header lookup is case-insensitive",
      ide_auth.session_token_from(None, {"x-sEsSiOn-tOkEn": "tok-2"}) == "tok-2")
check("Bearer token honoured",
      ide_auth.session_token_from(None, {"Authorization": "Bearer tok-3"}) == "tok-3")
check("bearer scheme is case-insensitive",
      ide_auth.session_token_from(None, {"authorization": "BeArEr tok-4"}) == "tok-4")
check("non-Bearer Authorization ignored (cookie fallback)",
      ide_auth.session_token_from("cookie", {"Authorization": "Basic xyz"}) == "cookie")
check("empty header value ignored (cookie fallback)",
      ide_auth.session_token_from("cookie", {"X-Session-Token": "   "}) == "cookie")
check("cookie used when no headers present",
      ide_auth.session_token_from("cookie", {}) == "cookie")
check("whitespace cookie ignored",
      ide_auth.session_token_from("   ", {}) is None)
check("nothing at all -> None",
      ide_auth.session_token_from(None, {}) is None)


# ── 3. CORS origin parsing ─────────────────────────────────────────
print("\nTest 3: parse_cors_origins — defaults, overrides, wildcard")

check("unset -> Tauri defaults",
      ide_auth.parse_cors_origins(None) == ide_auth.DEFAULT_CORS_ORIGINS,
      f"got={ide_auth.parse_cors_origins(None)}")
check("empty string -> Tauri defaults",
      ide_auth.parse_cors_origins("   ") == ide_auth.DEFAULT_CORS_ORIGINS)
check("explicit list is split and trimmed",
      ide_auth.parse_cors_origins("https://a.example, https://b.example")
      == ["https://a.example", "https://b.example"])
check("trailing slash trimmed",
      ide_auth.parse_cors_origins("https://a.example/") == ["https://a.example"])
check("wildcard disables origin checking",
      ide_auth.parse_cors_origins("*") == ["*"])
check("wildcard in a list wins",
      ide_auth.parse_cors_origins("https://a.example, *") == ["*"])
check("comma-only string falls back to defaults",
      ide_auth.parse_cors_origins(" , ,") == ide_auth.DEFAULT_CORS_ORIGINS)
check("a copy is returned (caller cannot mutate the default)",
      ide_auth.parse_cors_origins(None) is not ide_auth.DEFAULT_CORS_ORIGINS)


# ── 4. main.py wiring (compiled, not imported — FastAPI not installed) ──
print("\nTest 4: main.py wiring — every IDE integration point is threaded")

main_path = os.path.join(HERE, "web", "backend", "main.py")
with open(main_path, "r", encoding="utf-8") as f:
    source = f.read()

try:
    compile(source, main_path, "exec")
    check("main.py compiles", True)
except SyntaxError as exc:
    check("main.py compiles", False, f"line {exc.lineno}: {exc.msg}")

WIRING = [
    ("CORS middleware imported",
     "from fastapi.middleware.cors import CORSMiddleware"),
    ("CORS origins parsed via ide_auth",
     'parse_cors_origins(os.environ.get("WEB_CORS_ORIGINS"))'),
    ("CORS allowed headers come from ide_auth",
     "CORS_ALLOWED_HEADERS"),
    ("get_session_user resolves header credentials",
     'token = session_token_from(request.cookies.get("session"), request.headers)'),
    ("_stream_user resolves header credentials",
     'token = session_param or session_token_from('),
    ("login marks ?client=ide flows",
     "make_ide_state() if client"),
    ("callback detects the IDE flow",
     "is_ide_state(state)"),
    ("callback serves the IDE token page",
     "IDE_TOKEN_PAGE"),
]
for name, needle in WIRING:
    check(name, needle in source, f"missing: {needle!r}")

print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
