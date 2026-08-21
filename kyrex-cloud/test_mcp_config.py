"""MCP configuration delivery tests for serve.py.

Tests cover the three cases from KX_SERVE_DESIGN.md § MCP configuration:
  - Absent env var: write nothing, print to stderr
  - Valid JSON env var: write file to ~/.kyrex/mcp_servers.json
  - Malformed JSON env var: write nothing, print parse error to stderr

Uses a temporary directory instead of the real ~/.kyrex by patching
Path.home() via the MCP_SERVERS_DIR override — the tests import serve
normally but re-point the filesystem target.

Run: python3 test_mcp_config.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

# Import serve before monkey-patching so the module exists to patch.
# Also avoids a re-import after patch which might cache the old home.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "12345")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve

failures = []


def check(name, cond, detail=""):
    if cond:
        print("  PASS  " + name)
    else:
        print("  FAIL  " + name + (" " + detail if detail else ""))
        failures.append(name)


def reset_state(tmp_home: Path):
    """Reset the module-level MCP state and point it at a temp directory."""
    serve._MCP_WRITTEN = False
    serve.MCP_SERVERS_DIR = tmp_home / ".kyrex"
    serve.MCP_SERVERS_FILE = serve.MCP_SERVERS_DIR / "mcp_servers.json"
    # Clean up any leftover state in os.environ from previous tests
    os.environ.pop("MCP_SERVERS_JSON", None)


# ── Test 1: absent env var ──────────────────────────────────────────────────

print("\nTest 1: MCP_SERVERS_JSON absent — nothing written, stderr message")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    reset_state(tmp)
    os.environ.pop("MCP_SERVERS_JSON", None)

    # Capture stderr without injecting a file descriptor; use a pipe
    import io
    err_capture = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = err_capture
    try:
        serve.write_mcp_config()
    finally:
        sys.stderr = old_stderr

    err_text = err_capture.getvalue()
    check("stderr mentions unconfigured", "unconfigured" in err_text.lower(),
          f"got: {err_text.strip()}")
    check("no file created", not serve.MCP_SERVERS_FILE.exists())
    check("env var was absent", "MCP_SERVERS_JSON" not in os.environ)

# ── Test 2: valid JSON env var ─────────────────────────────────────────────

print("\nTest 2: valid MCP_SERVERS_JSON — file written with correct content")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    reset_state(tmp)

    sample = {"my-server": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}}
    os.environ["MCP_SERVERS_JSON"] = json.dumps(sample)

    serve.write_mcp_config()

    check("directory created", serve.MCP_SERVERS_DIR.exists())
    check("file exists", serve.MCP_SERVERS_FILE.exists())
    if serve.MCP_SERVERS_FILE.exists():
        loaded = json.loads(serve.MCP_SERVERS_FILE.read_text())
        check("content matches env", loaded == sample,
              f"expected {sample}, got {loaded}")

# ── Test 3: malformed JSON env var ─────────────────────────────────────────

print("\nTest 3: malformed MCP_SERVERS_JSON — nothing written, parse error to stderr")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    reset_state(tmp)

    os.environ["MCP_SERVERS_JSON"] = "{this is not valid json"

    err_capture = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = err_capture
    try:
        serve.write_mcp_config()
    finally:
        sys.stderr = old_stderr

    err_text = err_capture.getvalue()
    check("file not created", not serve.MCP_SERVERS_FILE.exists())
    check("parse error reported to stderr", "parse error" in err_text.lower()
          or "Expecting" in err_text or "JSONDecodeError" in err_text,
          f"got stderr: {err_text.strip()}")
    # Verify no partial write — there should be no file at all
    check("no partial file left behind",
          not serve.MCP_SERVERS_DIR.exists() or not serve.MCP_SERVERS_FILE.exists())

# ── Test 4: idempotency — calling twice does not re-write ──────────────────

print("\nTest 4: write_mcp_config is idempotent — second call does nothing")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    reset_state(tmp)

    sample = {"server": {"command": "echo"}}
    os.environ["MCP_SERVERS_JSON"] = json.dumps(sample)

    serve.write_mcp_config()
    mtime1 = serve.MCP_SERVERS_FILE.stat().st_mtime_ns

    # Second call should be a no-op (guarded by _MCP_WRITTEN)
    serve.write_mcp_config()
    mtime2 = serve.MCP_SERVERS_FILE.stat().st_mtime_ns

    check("file written once", mtime1 > 0)
    check("mtime unchanged on second call", mtime2 == mtime1,
          "-> second call rewrote the file")


# ── Summary ─────────────────────────────────────────────────────────────────

print("\n" + ("ALL TESTS PASSED" if not failures
              else "%d FAILURE(S): %s" % (len(failures), failures)))
sys.exit(1 if failures else 0)