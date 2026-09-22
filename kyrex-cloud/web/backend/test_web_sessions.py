import sys
import sqlite3
from pathlib import Path

CLOUD = Path(__file__).resolve().parents[2]
for path in (str(CLOUD), str(Path(__file__).resolve().parent)):
    if path not in sys.path:
        sys.path.insert(0, path)

from web_sessions import SessionStore


def test_session_survives_store_restart_without_persisting_bearer(tmp_path):
    path = tmp_path / "sessions.db"
    first = SessionStore(secret="stable-secret", path=path)
    first["raw-browser-cookie"] = "alice"

    restarted = SessionStore(secret="stable-secret", path=path)
    assert restarted.get("raw-browser-cookie") == "alice"
    assert b"raw-browser-cookie" not in path.read_bytes()


def test_session_expiry_and_logout_are_durable(tmp_path):
    now = [100.0]
    path = tmp_path / "sessions.db"
    store = SessionStore(
        secret="stable-secret", path=path, ttl_seconds=10,
        clock=lambda: now[0],
    )
    store["cookie"] = "alice"
    now[0] = 111.0
    assert store.get("cookie") is None
    assert SessionStore(
        secret="stable-secret", path=path, clock=lambda: now[0]
    ).get("cookie") is None

    store["other"] = "bob"
    del store["other"]
    assert store.get("other") is None


def test_session_digest_is_bound_to_server_secret(tmp_path):
    path = tmp_path / "sessions.db"
    SessionStore(secret="secret-a", path=path).__setitem__("cookie", "alice")
    assert SessionStore(secret="secret-b", path=path).get("cookie") is None
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT username FROM web_sessions").fetchall() == [
            ("alice",)
        ]
