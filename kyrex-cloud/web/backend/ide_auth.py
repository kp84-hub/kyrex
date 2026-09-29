#!/usr/bin/env python3
"""
ide_auth.py — Kyrex Cloud auth plumbing for desktop/IDE clients.

The browser frontend authenticates purely with the `session` cookie set by
/auth/callback. The Kyrex IDE (a Tauri desktop app) is a different origin
with no shared cookie jar, so it gets three small additions — all opt-in and
invisible to the browser flow:

  * `?client=ide` on /auth/login marks the OAuth state so /auth/callback
    responds with a page displaying the session token (for pasting into the
    IDE) instead of the cookie redirect to the web UI.
  * Header credentials: every endpoint that accepts the session cookie also
    accepts `X-Session-Token: <token>` or `Authorization: Bearer <token>`.
  * CORS: WEB_CORS_ORIGINS (comma-separated) whitelists the desktop app
    origin(s); it defaults to the packaged Tauri origins.

Everything here is dependency-free (stdlib only) so the IDE-facing logic is
unit-testable without FastAPI installed. main.py owns the HTTP wiring.
"""
import secrets
from typing import Mapping, Optional

# OAuth `state` prefix that marks a flow started by the IDE client.
IDE_STATE_PREFIX = "ide:"

# Default CORS origins: what a packaged Tauri v2 app presents, depending on
# platform (custom protocol on macOS/Linux, localhost scheme on Windows/
# WebView2). Override with WEB_CORS_ORIGINS, or set it to "*" to allow any
# origin (acceptable here: the API is single-operator by design).
DEFAULT_CORS_ORIGINS = [
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
]

# Headers the IDE client may send cross-origin.
CORS_ALLOWED_HEADERS = ["Content-Type", "X-Session-Token", "Authorization"]


def make_ide_state() -> str:
    """Return an OAuth `state` value marking an IDE-initiated flow."""
    return IDE_STATE_PREFIX + secrets.token_hex(16)


def is_ide_state(state: Optional[str]) -> bool:
    """True when *state* marks an IDE-initiated OAuth flow."""
    return bool(state) and state.startswith(IDE_STATE_PREFIX)


def session_token_from(
    cookie_token: Optional[str], headers: Mapping[str, str]
) -> Optional[str]:
    """Resolve the session token for one request.

    Order: explicit header credentials win (X-Session-Token, then Bearer),
    the browser cookie is the fallback. Header lookup is case-insensitive
    and empty/whitespace values are ignored, so a stale empty header can
    never shadow a valid cookie.
    """
    candidates = (("x-session-token", "plain"), ("authorization", "bearer"))
    for name, kind in candidates:
        value: Optional[str] = None
        for key, val in headers.items():
            if key.lower() == name:
                value = val
                break
        if not value:
            continue
        value = value.strip()
        if not value:
            continue
        if kind == "bearer":
            if not value.lower().startswith("bearer "):
                continue
            value = value[len("bearer "):].strip()
        if value:
            return value
    if cookie_token:
        cookie_token = cookie_token.strip()
        if cookie_token:
            return cookie_token
    return None


def parse_cors_origins(raw: Optional[str]) -> list:
    """Parse the WEB_CORS_ORIGINS env var into a CORS origin allowlist.

    Unset/empty -> the Tauri defaults. A bare "*" disables origin checking
    (CORSMiddleware's wildcard form). Trailing slashes are trimmed so
    "https://host/" matches the origin form "https://host" the browser
    actually sends.
    """
    if raw is None or not raw.strip():
        return list(DEFAULT_CORS_ORIGINS)
    parts = [p.strip().rstrip("/") for p in raw.split(",") if p.strip()]
    if "*" in parts:
        return ["*"]
    return parts or list(DEFAULT_CORS_ORIGINS)
