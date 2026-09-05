#!/usr/bin/env python3
"""dev/run_backend.py — local smoke-test launcher for Kyrex Chat (dev-only).

Runs the REAL, unmodified Kyrex Cloud backend app (kyrex-cloud/web/backend/
main.py — including the real chat_api router and chat_service) on
127.0.0.1:8000 with:

  * Provider env vars resolved from ~/.px/config.json (the same credentials
    the Kyrex engine uses). chat_service deliberately has no global-config
    fallback, so these are provided at launch as plain environment config —
    no backend code is changed.
  * One locally-seeded session ("session" cookie -> "local-smoke-user").
    This is exactly the browser-session mechanism the backend already uses
    (main.sessions / require_user); seeding it locally is the equivalent of
    an authenticated login and lets the standalone UI + smoke client talk to
    the real authenticated endpoints.
  * KYREX_DATA_DIR isolated under kyrex-chat/dev/data so the smoke test does
    not touch any other Kyrex state.

This file is dev tooling inside kyrex-chat/ and is not part of the shipped UI.
"""

import json
import os
import sys
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent          # kyrex-chat/dev/
CHAT_DIR = DEV_DIR.parent                          # kyrex-chat/
REPO_ROOT = CHAT_DIR.parent                        # repo root
BACKEND_DIR = REPO_ROOT / "kyrex-cloud" / "web" / "backend"
ENGINE_DIR = REPO_ROOT / "kyrex_engine"

for p in (str(BACKEND_DIR), str(ENGINE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# ── provider configuration (launch-time env, from the engine's own config) ──
_px_cfg = {}
_px_path = Path.home() / ".px" / "config.json"
if _px_path.exists():
    try:
        _px_cfg = json.loads(_px_path.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke] warning: could not read {_px_path}: {exc}")

os.environ.setdefault("KYREX_PROVIDER", _px_cfg.get("provider", "openai"))
os.environ.setdefault("KYREX_MODEL", _px_cfg.get("model", ""))
os.environ.setdefault("KYREX_API_KEY", _px_cfg.get("api_key", ""))
os.environ.setdefault("KYREX_BASE_URL", _px_cfg.get("base_url", ""))

# Isolate the smoke test's chat/task data under kyrex-chat/dev/data.
os.environ.setdefault("KYREX_DATA_DIR", str(DEV_DIR / "data"))

# ── local workspace registration (dev-only desktop selection point) ──
# `python dev/run_backend.py --workspace /path/to/repo` registers that
# directory as workspace id "default" in the server-side registry. This is
# the LOCAL equivalent of the desktop selection: the *process owner* chooses
# the directory at launch — a browser request can never submit a filesystem
# path (the API only accepts registry ids). Cloud deployments set
# KYREX_CHAT_WORKSPACES to server-side clones instead.
if "--workspace" in sys.argv:
    _ws_idx = sys.argv.index("--workspace")
    if _ws_idx + 1 >= len(sys.argv):
        sys.exit("run_backend.py: --workspace requires a directory argument")
    _ws_dir = Path(sys.argv[_ws_idx + 1]).expanduser().resolve()
    if not _ws_dir.is_dir():
        sys.exit(f"run_backend.py: workspace is not a directory: {_ws_dir}")
    os.environ["KYREX_CHAT_WORKSPACES"] = json.dumps({"default": str(_ws_dir)})
    print(f"[smoke] workspace registered: default -> {_ws_dir}")

# Placeholder OAuth env: the OAuth routes are not exercised by the smoke
# test, but main.py requires these keys at import time.
os.environ.setdefault("GITHUB_CLIENT_ID", "local-dev")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "local-dev")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "local-smoke-user")

# ── real backend app ───────────────────────────────────────────────
import main as backend_main  # noqa: E402
import chat_service  # noqa: E402  (imported into chat_api's namespace by main)
import uvicorn  # noqa: E402

SESSION_TOKEN = "kyrex-chat-local-smoke-session"
SESSION_USER = "local-smoke-user"
# Seed the session exactly the way a successful browser login does.
backend_main.sessions[SESSION_TOKEN] = SESSION_USER

if __name__ == "__main__":
    ok, detail = chat_service.engine_available()
    print(f"[smoke] engine_available: {ok} ({detail})")
    print(f"[smoke] session cookie: session={SESSION_TOKEN}")
    print("[smoke] data dir:", os.environ["KYREX_DATA_DIR"])
    uvicorn.run(backend_main.app, host="127.0.0.1", port=8000, log_level="warning")
