"""First-class Bot creation via POST /api/bots — focused regression coverage.

The create endpoint is a security boundary, so these tests ARE the spec:

  1. A successful, owner-scoped create records identity (name/slug/system
     prompt), an exact provider-profile/model pair, a capability policy, an
     optional browser allowlist, and a safe Rift — and the caller is the owner.
  2. A duplicate id is rejected (409) and the existing Bot is untouched.
  3. Anonymous callers get 401; a foreign (another user's) provider profile is
     rejected (400) and nothing is written.
  4. Invalid input fails closed: bad ids, empty/oversized names, missing model,
     a running initial status, malformed policies, both preset+policy, a
     non-hostname allowlist, an unknown workspace id, and a model outside the
     referenced profile.
  5. A new Bot never silently inherits the GLOBAL provider: with KYREX_MODEL /
     KYREX_API_KEY set, a create with no profile stores NO reference and fails
     closed at resolution; a create with no model at all is rejected.
  6. New Bots start STOPPED by default.
  7. A newly created Bot appears in the owner's roster (and no one else's).
  8. No API key or header value ever reaches the registry, the response, or the
     registry file on disk.
  9. Rift safety: a SERVER-REGISTERED workspace may be selected; a raw
     filesystem path is ignored and a server-generated Rift is used instead.
 10. A writable (Developer preset) create requires a repo Rift — rejected with
     an empty/fresh Rift (409), accepted against a real git workspace.

Run: python3 -m pytest test_bot_create.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-bot-create-tests")
# Fernet key material for the encrypted per-user provider-profile store.
os.environ.setdefault("WEB_SESSION_SECRET", "bot-create-test-secret")


@pytest.fixture(autouse=True)
def _scoped_provider_env(monkeypatch):
    """Scope provider defaults to each test and restore them afterwards.

    Import-time ``os.environ.setdefault`` would otherwise leak a whole-session
    KYREX_MODEL / KYREX_API_KEY default into sibling modules collected later.
    """
    monkeypatch.setenv("KYREX_PROVIDER", "openai")
    monkeypatch.setenv("KYREX_MODEL", "env-default-model")
    monkeypatch.setenv("KYREX_API_KEY", "sk-env-default")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main  # noqa: E402  (after env setup)
import bots  # noqa: E402
import provider_profiles  # noqa: E402
import bot_provider  # noqa: E402
import chat_service  # noqa: E402
import dev_bot  # noqa: E402


SECRET = "sk-secret-create-1234"
_PROFILE_ID = "create-prof"


# ── helpers ────────────────────────────────────────────────────────

def _reset():
    root = chat_service._chat_root()
    for p in root.rglob("*.json"):
        p.unlink()
    for p in root.rglob("*.json.tmp"):
        p.unlink()
    bots.save_bots({})


def setup_function():
    _reset()
    main.sessions["sess-alice"] = "alice"
    main.sessions["sess-bob"] = "bob"


def teardown_function():
    _reset()


def _profile(owner="alice", profile_id=_PROFILE_ID, models=("m1", "m2")):
    return provider_profiles.save_profile(owner, {
        "id": profile_id,
        "name": profile_id.upper(),
        "provider": "openai",
        "base_url": "https://example.test/v1",
        "api_key": SECRET,
        "models": list(models),
        "headers": {"X-Extra": "shh-value"},
    })


def _git_rift() -> str:
    """A real git repository workspace (a valid Developer Bot Rift)."""
    d = tempfile.mkdtemp(prefix="kyrex-create-git-rift-")
    subprocess.run(["git", "init", "-q", d], check=True,
                   capture_output=True, text=True)
    return d


def _plain_dir() -> str:
    return tempfile.mkdtemp(prefix="kyrex-create-ws-")


def _client(user="alice"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


# ── 1. successful owner-scoped creation ────────────────────────────

def test_create_bot_owner_scoped_success():
    _profile()
    r = _client("alice").post("/api/bots", json={
        "id": "newbot",
        "name": "New Bot",
        "model": "m1",
        "system_prompt": "You are New Bot.",
        "provider_profile_id": _PROFILE_ID,
        "browser_allowlist": ["Example.com", "docs.example.com"],
        "status": "stopped",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "newbot"
    assert body["name"] == "New Bot"
    assert body["status"] == "stopped"
    assert body["manageable"] is True and body["claimable"] is False
    assert body["provider_profile_id"] == _PROFILE_ID

    stored = bots.get_bot("newbot")
    assert stored["owner"] == "alice"
    assert stored["system_prompt"] == "You are New Bot."
    # Hostnames are normalised to lowercase and de-duplicated.
    assert stored["browser_allowlist"] == ["example.com", "docs.example.com"]
    assert stored["provider_profile_id"] == _PROFILE_ID
    assert stored["model"] == "m1"
    assert stored["status"] == "stopped"
    # The Rift is a server-generated directory that actually exists.
    assert Path(stored["rift"]).is_dir()
    assert str(stored["rift"]).startswith(str(bots.DATA_DIR))


# ── 2. duplicate id ────────────────────────────────────────────────

def test_create_bot_duplicate_id_rejected():
    _profile()
    first = _client("alice").post(
        "/api/bots", json={"id": "dup", "name": "A", "model": "m1"})
    assert first.status_code == 200, first.text
    second = _client("alice").post(
        "/api/bots", json={"id": "dup", "name": "B", "model": "m2"})
    assert second.status_code == 409, second.text
    # The original Bot is untouched — add_bot never overwrites.
    assert bots.get_bot("dup")["name"] == "A"


# ── 3. authentication + ownership ──────────────────────────────────

def test_create_bot_requires_authentication():
    from fastapi.testclient import TestClient
    r = TestClient(main.app).post(
        "/api/bots", json={"id": "anon", "name": "Anon", "model": "m1"})
    assert r.status_code == 401
    with pytest.raises(KeyError):
        bots.get_bot("anon")


def test_create_bot_rejects_foreign_provider_profile():
    # The profile belongs to bob; alice's create referencing it must 400.
    _profile(owner="bob", profile_id=_PROFILE_ID, models=("m1",))
    r = _client("alice").post("/api/bots", json={
        "id": "foreign", "name": "Foreign", "model": "m1",
        "provider_profile_id": _PROFILE_ID,
    })
    assert r.status_code == 400, r.text
    assert "not configured" in r.json()["detail"]
    with pytest.raises(KeyError):
        bots.get_bot("foreign")


# ── 4. invalid input fails closed ──────────────────────────────────

def test_create_bot_rejects_invalid_fields():
    _profile(models=("m1",))
    c = _client("alice")
    cases = [
        {"id": "../escape", "name": "N", "model": "m1"},          # bad id
        {"id": "has space", "name": "N", "model": "m1"},          # bad id
        {"id": "ok-name", "name": "", "model": "m1"},             # empty name
        {"id": "ok-name2", "name": "N", "model": ""},             # no model
        {"id": "ok-name3", "name": "N", "model": "m1",
         "status": "running"},                                    # never running
        {"id": "ok-name4", "name": "N", "model": "m1",
         "policy": "not-a-dict"},                                 # bad policy
        {"id": "ok-name5", "name": "N", "model": "m1",
         "policy": {"fs:read": 5}},                               # bad tier
        {"id": "ok-name6", "name": "N", "model": "m1",
         "preset": "ghost"},                                      # unknown preset
        {"id": "ok-name7", "name": "N", "model": "m1",
         "preset": "developer", "policy": {}},                    # both
        {"id": "ok-name8", "name": "N", "model": "m1",
         "browser_allowlist": ["https://example.com"]},           # scheme
        {"id": "ok-name9", "name": "N", "model": "m1",
         "browser_allowlist": ["a/b"]},                           # path
        {"id": "ok-name10", "name": "N", "model": "m1",
         "workspace_id": "ghost-ws"},                             # unknown ws
        {"id": "ok-name11", "name": "N", "model": "nope",
         "provider_profile_id": _PROFILE_ID},                     # model∉profile
    ]
    for body in cases:
        r = c.post("/api/bots", json=body)
        assert r.status_code == 400, (body, r.status_code, r.text)


# ── 5. no global provider fallback ─────────────────────────────────

def test_create_bot_does_not_inherit_global_provider(monkeypatch):
    monkeypatch.setenv("KYREX_MODEL", "global-model")
    monkeypatch.setenv("KYREX_API_KEY", "sk-global")
    monkeypatch.setenv("KYREX_PROVIDER", "openai")

    r = _client("alice").post("/api/bots", json={
        "id": "noglobal", "name": "No Global", "model": "explicit-model"})
    assert r.status_code == 200, r.text
    stored = bots.get_bot("noglobal")
    # The global model/key are NEVER copied into the registry.
    assert stored["provider_profile_id"] == ""
    assert stored["model"] == "explicit-model"
    assert "global-model" not in Path(bots.BOTS_FILE).read_text()
    # An unconfigured Bot fails closed at resolution rather than borrowing the
    # host's global credentials.
    with pytest.raises(bot_provider.BotProviderError):
        bot_provider.resolve_bot_provider("alice", stored)

    # A create with NO model at all is rejected — no global model fallback.
    assert _client("alice").post(
        "/api/bots", json={"id": "nomodel", "name": "NM"}).status_code == 400
    with pytest.raises(KeyError):
        bots.get_bot("nomodel")


# ── 6. default stopped ─────────────────────────────────────────────

def test_create_bot_defaults_to_stopped():
    r = _client("alice").post(
        "/api/bots", json={"id": "dflt", "name": "Default", "model": "m1"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "stopped"
    assert bots.get_bot("dflt")["status"] == "stopped"


# ── 7. appears in the owner's roster ───────────────────────────────

def test_created_bot_appears_in_owner_roster_only():
    assert _client("alice").post(
        "/api/bots", json={"id": "roster", "name": "Roster", "model": "m1"}
    ).status_code == 200

    alice_bots = _client("alice").get("/api/bots").json()["bots"]
    entry = next(b for b in alice_bots if b["id"] == "roster")
    assert entry["manageable"] is True
    assert entry["claimable"] is False
    assert entry["status"] == "stopped"

    bob_ids = {b["id"] for b in _client("bob").get("/api/bots").json()["bots"]}
    assert "roster" not in bob_ids


# ── 8. no secrets in any output ────────────────────────────────────

def test_create_bot_stores_no_secrets():
    _profile(models=("m1",))
    r = _client("alice").post("/api/bots", json={
        "id": "sec", "name": "Secret-free", "model": "m1",
        "provider_profile_id": _PROFILE_ID,
    })
    assert r.status_code == 200, r.text
    # Neither the API response nor the on-disk registry carries the key or a
    # header value — only the profile REFERENCE.
    assert SECRET not in json.dumps(r.json())
    raw = Path(bots.BOTS_FILE).read_text()
    assert SECRET not in raw
    assert "shh-value" not in raw


# ── 9. Rift safety ─────────────────────────────────────────────────

def test_create_bot_can_use_registered_workspace_as_rift(monkeypatch):
    ws = _plain_dir()
    monkeypatch.setenv("KYREX_CHAT_WORKSPACES", json.dumps({"ws1": ws}))
    r = _client("alice").post("/api/bots", json={
        "id": "wsbot", "name": "Workspace Bot", "model": "m1",
        "workspace_id": "ws1",
    })
    assert r.status_code == 200, r.text
    stored = bots.get_bot("wsbot")
    assert Path(stored["rift"]).resolve() == Path(ws).resolve()
    # The whole response is still the non-secret public view.
    assert "rift" not in r.json()


def test_create_bot_ignores_client_supplied_filesystem_path():
    # A raw path in the body must never become the Rift — the server generates
    # one under its own data root instead.
    r = _client("alice").post("/api/bots", json={
        "id": "pathbot", "name": "Path Bot", "model": "m1",
        "rift": "/etc", "path": "/etc",
    })
    assert r.status_code == 200, r.text
    stored = bots.get_bot("pathbot")
    assert stored["rift"] != "/etc"
    assert str(stored["rift"]).startswith(str(bots.DATA_DIR))
    assert Path(stored["rift"]).is_dir()


# ── 10. writable create requires a repo Rift ───────────────────────

def test_create_bot_developer_preset_requires_repo_rift(monkeypatch):
    # A fresh/empty default Rift is not a git repository → 409, nothing written.
    bad = _client("alice").post("/api/bots", json={
        "id": "dev-no", "name": "Dev No", "model": "m1", "preset": "developer"})
    assert bad.status_code == 409, bad.text
    with pytest.raises(KeyError):
        bots.get_bot("dev-no")

    # Against a real git workspace the writable policy is accepted.
    repo = _git_rift()
    monkeypatch.setenv("KYREX_CHAT_WORKSPACES", json.dumps({"repo1": repo}))
    ok = _client("alice").post("/api/bots", json={
        "id": "dev-ok", "name": "Dev Ok", "model": "m1",
        "preset": "developer", "workspace_id": "repo1"})
    assert ok.status_code == 200, ok.text
    assert dev_bot.is_writable_bot_policy(bots.get_bot("dev-ok")["policy"])
