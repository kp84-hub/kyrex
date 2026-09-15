"""Phase 2 browser host Cloud wiring — endpoint tests over the REAL FastAPI app.

These mount the same production routes ``main.py`` mounts and drive them
through ``fastapi.testclient.TestClient(main.app)``, proving the deployable
surface end to end:

  POST   /api/browser-hosts         enroll (mints the secret ONCE)
  GET    /api/browser-hosts         owner-scoped list (+ WSS endpoint)
  GET    /api/browser-hosts/endpoint  the wss:// URL the host dials
  GET    /api/browser-hosts/{id}    status (owner-scoped)
  DELETE /api/browser-hosts/{id}    revoke
  WS     /api/browser-hosts/ws      HMAC-authenticated host channel

Covered: enrollment; host connect (HMAC hello -> hello_ok); heartbeat ->
online; rejection (bad proof -> hello_err, host stays offline); unavailable
state (disconnect); revoke; owner isolation; and that NO response ever leaks
the enrollment secret except the one-time mint, nor any CDP URL.

Run: python3 -m pytest test_browser_host_api.py
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ["GITHUB_CLIENT_ID"] = os.environ.get("GITHUB_CLIENT_ID", "test-client")
os.environ["GITHUB_CLIENT_SECRET"] = os.environ.get("GITHUB_CLIENT_SECRET", "test-secret")
os.environ["WEB_ALLOWED_GITHUB_USERNAME"] = os.environ.get(
    "WEB_ALLOWED_GITHUB_USERNAME", "owner")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-browser-host-api-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "browser-host-api-test-secret")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main                      # noqa: E402  (seeds the shared app + session map)
import browser_host_api          # noqa: E402  — the module under test
import browser_host_channel as channel_mod  # noqa: E402
import browser_hosts as hosts    # noqa: E402

HOST = "vps-browser-1"
OWNER = "owner"


# ── fixtures ──────────────────────────────────────────────────────────

def _reset():
    data = Path(os.environ["KYREX_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    try:
        (data / "browser_hosts.json").unlink()
    except OSError:
        pass
    # Drop any live channels so state never bleeds between tests.
    mgr = channel_mod.default_manager()
    for host_id in list(mgr._channels.keys()):  # noqa: SLF001 — test cleanup
        ch = mgr.channel_for(host_id)
        if ch is not None:
            mgr.detach(ch)


def setup_function():
    _reset()
    main.sessions["sess-owner"] = "owner"
    main.sessions["sess-other"] = "other"


def teardown_function():
    _reset()


def _client(user=OWNER):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


def _frame(type_, payload):
    return {"v": 1, "type": type_, "id": None, "ts": 0.0, "payload": payload}


def _enroll(client, host_id=HOST, **extra):
    body = {"host_id": host_id, **extra}
    return client.post("/api/browser-hosts", json=body)


def _walk_strings(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)
    else:
        yield str(obj)


# ── 1. auth boundary ──────────────────────────────────────────────────

def test_enroll_requires_authentication():
    from fastapi.testclient import TestClient
    resp = TestClient(main.app).post("/api/browser-hosts", json={"host_id": HOST})
    assert resp.status_code == 401


# ── 2. enrollment mints the secret ONCE ───────────────────────────────

def test_enroll_mints_secret_once_and_never_again():
    client = _client()
    first = _enroll(client)
    assert first.status_code == 200, first.text
    payload = first.json()
    secret = payload["secret"]
    assert isinstance(secret, str) and len(secret) >= 20
    assert payload["host"]["host_id"] == HOST
    assert payload["host"]["has_credential"] is True

    # The second enrollment PRESERVES the secret and returns none.
    second = _enroll(client, name="renamed")
    assert second.status_code == 200
    assert second.json()["secret"] is None
    assert second.json()["host"]["name"] == "renamed"

    # ...and the secret is nowhere in any later response.
    everything = second.text + client.get("/api/browser-hosts").text + \
        client.get(f"/api/browser-hosts/{HOST}").text
    assert secret not in everything


def test_client_supplied_secret_is_ignored():
    client = _client()
    minted = _enroll(client).json()["secret"]
    # A caller that tries to SET the secret gets it ignored (owner preserved).
    resp = _enroll(client, secret="attacker-chosen")
    assert resp.json()["secret"] is None
    # The host still authenticates with the ORIGINAL minted secret.
    assert hosts.verify_proof(HOST, "n", hosts.proof_for(minted, HOST, "n")) is True
    assert hosts.verify_proof(
        HOST, "n", hosts.proof_for("attacker-chosen", HOST, "n")) is False


def test_enroll_validation_and_cross_owner():
    client = _client()
    assert _enroll(client, host_id="bad/id").status_code == 400
    assert _enroll(client, host_id=HOST).status_code == 200
    # A DIFFERENT owner cannot take the same host id.
    other = _client("other")
    assert _enroll(other, host_id=HOST).status_code == 409


# ── 3. list / status are redacted and owner-scoped ────────────────────

def test_list_and_status_are_secret_free():
    client = _client()
    secret = _enroll(client).json()["secret"]
    listed = client.get("/api/browser-hosts").json()
    assert [h["host_id"] for h in listed["hosts"]] == [HOST]
    for text in list(_walk_strings(listed)):
        assert secret not in text
        assert "devtools" not in text
        assert "ws://" not in text  # never a plaintext/loopback ws target


def test_status_is_owner_scoped():
    _client().post("/api/browser-hosts", json={"host_id": HOST})
    other = _client("other")
    assert other.get(f"/api/browser-hosts/{HOST}").status_code == 404
    assert other.delete(f"/api/browser-hosts/{HOST}").status_code == 404


# ── 4. WSS endpoint construction (Railway) ────────────────────────────

def test_endpoint_is_a_wss_url():
    client = _client()
    body = client.get("/api/browser-hosts/endpoint").json()
    assert body["wss_url"].startswith("wss://")
    assert body["wss_url"].endswith(browser_host_api.WS_PATH)
    assert body["env_var"] == "KYREX_HOST_CLOUD_URL"


def test_wss_url_coerces_http_and_ws_up_to_wss():
    assert browser_host_api.wss_url("https://k.up.railway.app") == \
        f"wss://k.up.railway.app{browser_host_api.WS_PATH}"
    assert browser_host_api.wss_url("http://k.up.railway.app/") == \
        f"wss://k.up.railway.app{browser_host_api.WS_PATH}"
    assert browser_host_api.wss_url("ws://k") == f"wss://k{browser_host_api.WS_PATH}"


# ── 5. host connect + heartbeat over the real app ─────────────────────

def _hello(secret, host_id=HOST, nonce="n-1"):
    return _frame("hello", {
        "host_id": host_id, "owner": OWNER, "nonce": nonce,
        "proof": hosts.proof_for(secret, host_id, nonce), "protocol": 1,
    })


def _poll(predicate, timeout=3.0):
    """Poll a predicate over the durable registry (no HTTP while a WS is open)."""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = predicate()
        if last:
            return last
        time.sleep(0.02)
    return last


def test_host_connects_and_heartbeat_marks_online():
    client = _client()
    secret = _enroll(client).json()["secret"]

    with client.websocket_connect(browser_host_api.WS_PATH) as ws:
        ws.send_json(_hello(secret))
        ok = ws.receive_json()
        assert ok["type"] == "hello_ok"
        assert ok["payload"]["protocol"] == 1

        # The Cloud registered a live, authenticated channel for this host.
        assert _poll(lambda: channel_mod.default_manager().channel_for(HOST))
        assert channel_mod.default_manager().channel_for(HOST).authenticated is True
        assert hosts.get_host(HOST).effective_state() == hosts.STATE_ONLINE

        # A heartbeat ADVANCES last_seen (so it is the heartbeat being seen,
        # not just the hello). HTTP is deliberately not called while the socket
        # is open.
        seen_before = hosts.get_host(HOST).last_seen_at
        ws.send_json(_frame("heartbeat", {"state": "idle", "host_id": HOST}))
        assert _poll(lambda: hosts.get_host(HOST).last_seen_at > seen_before)


# ── 6. rejection ──────────────────────────────────────────────────────

def test_bad_proof_is_rejected_and_host_stays_offline():
    client = _client()
    _enroll(client)  # mint a secret we will NOT use

    with client.websocket_connect(browser_host_api.WS_PATH) as ws:
        ws.send_json(_frame("hello", {
            "host_id": HOST, "owner": OWNER, "nonce": "n-1",
            "proof": "not-the-right-proof", "protocol": 1,
        }))
        reply = ws.receive_json()
        assert reply["type"] == "hello_err"

    status = client.get(f"/api/browser-hosts/{HOST}").json()["host"]
    assert status["connected"] is False
    assert status["available"] is False


def test_unknown_host_proof_is_rejected():
    client = _client()
    with client.websocket_connect(browser_host_api.WS_PATH) as ws:
        ws.send_json(_frame("hello", {
            "host_id": "never-enrolled", "owner": OWNER, "nonce": "n",
            "proof": "x", "protocol": 1,
        }))
        assert ws.receive_json()["type"] == "hello_err"


# ── 7. unavailable state (disconnect) ─────────────────────────────────

def test_disconnect_marks_host_unavailable_and_fails_closed():
    client = _client()
    secret = _enroll(client).json()["secret"]

    with client.websocket_connect(browser_host_api.WS_PATH) as ws:
        ws.send_json(_hello(secret))
        assert ws.receive_json()["type"] == "hello_ok"
        assert _poll(lambda: channel_mod.default_manager().channel_for(HOST)
                     is not None)
        assert channel_mod.default_manager().channel_for(HOST).authenticated is True

    # After the socket closes the channel is DROPPED, so routing fails closed...
    assert _poll(lambda: channel_mod.default_manager().channel_for(HOST)
                 is None) is True
    # ...and the host reports unavailable over the real HTTP surface.
    body = client.get(f"/api/browser-hosts/{HOST}").json()["host"]
    assert body["state"] == "unavailable"
    assert body["connected"] is False
    assert body["available"] is False


# ── 8. revoke ─────────────────────────────────────────────────────────

# ── 9. app lifecycle ──────────────────────────────────────────────────

def test_lifespan_starts_and_stops_the_channel_manager():
    from fastapi.testclient import TestClient
    mgr = channel_mod.default_manager()
    mgr.stop()
    with TestClient(main.app) as _client_ctx:
        assert mgr._sweeper is not None            # noqa: SLF001 — lifecycle probe
        assert mgr._sweeper.is_alive() is True
    # Shutdown tore the sweeper down cleanly.
    assert mgr._sweeper is None                    # noqa: SLF001


def test_manager_start_stop_is_idempotent():
    mgr = channel_mod.HostManager()
    assert mgr.start() is mgr
    assert mgr.start() is mgr                      # second start is a no-op
    assert mgr._sweeper.is_alive() is True         # noqa: SLF001
    mgr.stop()
    mgr.stop()                                     # idempotent
    assert mgr._sweeper is None                    # noqa: SLF001


# ── 8. revoke ─────────────────────────────────────────────────────────

def test_revoke_is_terminal():
    client = _client()
    _enroll(client)
    resp = client.delete(f"/api/browser-hosts/{HOST}")
    assert resp.status_code == 200 and resp.json()["revoked"] is True

    # Gone from the owner's list, status is a 404, and its secret is erased.
    assert client.get("/api/browser-hosts").json()["hosts"] == []
    assert client.get(f"/api/browser-hosts/{HOST}").status_code == 404
    assert hosts.verify_proof(HOST, "n", "anything") is False


# ── 10. runtime guard: non-WebSocket GET on the WS path ───────────────

def test_non_websocket_get_on_ws_path_is_426_not_401():
    """A plain HTTP GET to the WS path is a clear Upgrade Required (426).

    Regression: when the runtime has no WebSocket implementation (uvicorn
    without ``websockets``) the upgrade is not performed and the handshake
    arrives as HTTP. It must NOT fall into the session-guarded
    ``GET /{host_id}`` route and be answered 401 — a transport fault must never
    be misreported as an authentication failure.
    """
    from fastapi.testclient import TestClient
    # Anonymous: previously 401 {"detail": "Not authenticated"}.
    anon = TestClient(main.app)
    resp = anon.get(browser_host_api.WS_PATH)
    assert resp.status_code == 426, resp.text
    assert resp.headers.get("upgrade", "").lower() == "websocket"
    assert "upgrade" in resp.json()["detail"].lower()
    # Owner-authenticated: also 426 (never the host-status route).
    assert _client().get(browser_host_api.WS_PATH).status_code == 426


def test_ws_path_guard_does_not_shadow_owner_routes():
    """The guard is scoped to the literal ``/ws``; real host ids still work."""
    client = _client()
    _enroll(client)
    assert client.get("/api/browser-hosts").status_code == 200
    assert client.get("/api/browser-hosts/endpoint").status_code == 200
    assert client.get(f"/api/browser-hosts/{HOST}").status_code == 200


def test_real_websocket_handshake_reaches_hmac_hello_handler():
    """A genuine upgrade still reaches the HMAC ``hello`` handler.

    ``hello_err`` for a bad proof (rather than the 426 HTTP guard) proves the
    new HTTP route captured only HTTP scope and left WebSocket scope intact.
    """
    client = _client()
    _enroll(client)
    with client.websocket_connect(browser_host_api.WS_PATH) as ws:
        ws.send_json(_frame("hello", {
            "host_id": HOST, "owner": OWNER, "nonce": "n-1",
            "proof": "not-the-right-proof", "protocol": 1,
        }))
        assert ws.receive_json()["type"] == "hello_err"


# ── 11. runtime dependency: websockets must be declared ───────────────

def _pip_install_tokens(dockerfile: Path) -> list[str]:
    """All tokens of the Dockerfile's pip-install RUN layer.

    Joins backslash-continuation lines so a multi-line ``pip install`` (e.g.
    ``kyrex-cloud/web/Dockerfile``) is checked in full, not just its first
    physical line, and so the dependency can appear on any continuation.
    """
    lines = dockerfile.read_text().splitlines()
    install: list[str] = []
    in_install = False
    for ln in lines:
        if install and not in_install:
            break
        if ln.strip().startswith(("RUN pip install", "pip install")):
            in_install = True
        if in_install:
            install.extend(ln.replace("\\", "").split())
            if not ln.rstrip().endswith("\\"):
                break
    return install


def test_cloud_images_declare_websockets_dependency():
    """Both Cloud Dockerfiles must declare ``websockets`` explicitly.

    Regression: without a websocket implementation uvicorn cannot perform the
    Browser Host upgrade, so the handshake degrades to HTTP (production
    reproduced this as a live 426 from Railway even after the kyrex-cloud /
    Dockerfile fix, because the service builds kyrex-cloud/web/Dockerfile).
    Both images build the same uvicorn runtime with the same browser-host
    routes, so BOTH must declare the dependency rather than rely on an
    ambient/transitive package.
    """
    for name in ("Dockerfile", os.path.join("web", "Dockerfile")):
        dockerfile = Path(_CLOUD) / name
        assert dockerfile.exists(), f"missing Cloud Dockerfile: {name}"
        install = _pip_install_tokens(dockerfile)
        assert install, f"{name} must pip-install its runtime"
        assert "websockets" in install, (
            f"kyrex-cloud/{name} must declare the 'websockets' dependency "
            "explicitly: without it uvicorn cannot upgrade the Browser Host "
            "WebSocket and the handshake degrades to HTTP."
        )
