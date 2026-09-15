"""Tests for the Cloud-side Browser Host registry (``browser_hosts.py``).

Pins the contract the secure Cloud <-> Host channel relies on:

  1. enrollment / revocation — a secret is minted once, stored SEALED, and
     never returned again; a foreign owner can never enroll another's id.
  2. authentication — the HMAC possession proof validates, and fails closed
     for a wrong proof, unknown host, revoked host, or missing secret.
  3. liveness — heartbeat keeps a host online; a stale heartbeat sweeps it to
     ``unavailable``; availability is fail-closed.
  4. isolation — hosts and bindings are owner-scoped; a foreign host id is
     refused; ambiguity resolves to ``None`` (never a guess).
  5. redaction — CDP endpoints and secrets are scrubbed from every view.
  6. fail closed — with no seal key configured, enrollment raises and writes
     nothing.

Deterministic: every liveness call is driven by an explicit ``now``.

Run: python3 -m pytest test_browser_hosts.py
"""
import json
import os
import sys

import pytest

_CLOUD = os.path.dirname(os.path.abspath(__file__))
if _CLOUD not in sys.path:
    sys.path.insert(0, _CLOUD)

import browser_hosts as bh  # noqa: E402

SECRET_KEY = "host-registry-test-secret"
T = 1_700_000_000.0


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_SESSION_SECRET", SECRET_KEY)
    monkeypatch.delenv("KYREX_PROVIDER_SECRETS_KEY", raising=False)
    monkeypatch.delenv("KYREX_HOST_HEARTBEAT_INTERVAL", raising=False)
    monkeypatch.delenv("KYREX_HOST_HEARTBEAT_TIMEOUT", raising=False)
    yield tmp_path


def _raw_registry():
    return json.loads(bh._registry_path().read_text())


# ── 1. enrollment / revocation ────────────────────────────────────────

def test_enroll_mints_secret_once_and_seals_it():
    out = bh.enroll_host("owner", "host-1", name="box", now=T)
    secret = out["secret"]
    assert secret and len(secret) >= 20
    assert out["host"]["host_id"] == "host-1"
    assert out["host"]["has_credential"] is True

    raw = bh._registry_path().read_text()
    assert secret not in raw, "the enrollment secret must never be plaintext"
    sealed = _raw_registry()["hosts"]["host-1"]["sealed"]
    assert sealed and secret not in sealed
    assert bh.unseal_secret(sealed) == secret


def test_re_enroll_without_secret_keeps_the_old_one():
    first = bh.enroll_host("owner", "host-1", now=T)
    secret = first["secret"]
    again = bh.enroll_host("owner", "host-1", name="renamed", now=T + 1)
    assert again["secret"] is None
    assert bh.unseal_secret(_raw_registry()["hosts"]["host-1"]["sealed"]) == secret
    assert again["host"]["name"] == "renamed"


def test_re_enroll_can_rotate_the_secret():
    bh.enroll_host("owner", "host-1", now=T)
    rotated = bh.enroll_host("owner", "host-1", secret="brand-new-secret", now=T + 1)
    assert rotated["secret"] is None
    assert bh.unseal_secret(_raw_registry()["hosts"]["host-1"]["sealed"]) == "brand-new-secret"


def test_enroll_rejects_foreign_owner_for_a_taken_id():
    bh.enroll_host("alice", "host-1", now=T)
    with pytest.raises(bh.HostError):
        bh.enroll_host("bob", "host-1", now=T)


def test_enroll_rejects_bad_ids_and_allowlists():
    for bad in ("", "a/b", "a b", "x" * 65, ".."):
        with pytest.raises(bh.HostError):
            bh.enroll_host("owner", bad, now=T)
    with pytest.raises(bh.HostError):
        bh.enroll_host("owner", "host-1", allowlist=["https://example.com/x"], now=T)
    with pytest.raises(bh.HostError):
        bh.enroll_host("owner", "host-1", allowlist="example.com", now=T)


def test_revoke_is_terminal_and_drops_the_secret_and_bindings():
    bh.enroll_host("owner", "host-1", now=T)
    bh.bind_bot("owner", "bot", "host-1", now=T)
    assert bh.revoke_host("owner", "host-1") is True
    assert bh.revoke_host("owner", "host-1") is False           # idempotent
    assert bh.get_host("host-1", now=T).state == bh.STATE_REVOKED
    assert _raw_registry()["hosts"]["host-1"]["sealed"] is None
    assert _raw_registry()["bindings"].get("owner", {}) == {}
    assert bh.verify_proof("host-1", "n", "whatever") is False


def test_revoke_requires_the_owner():
    bh.enroll_host("alice", "host-1", now=T)
    assert bh.revoke_host("bob", "host-1") is False
    assert bh.get_host("host-1", now=T).state != bh.STATE_REVOKED


# ── 2. authentication ─────────────────────────────────────────────────

def test_verify_proof_accepts_the_right_hmac_and_rejects_the_rest():
    secret = bh.enroll_host("owner", "host-1", now=T)["secret"]
    good = bh.proof_for(secret, "host-1", "nonce-1")
    assert bh.verify_proof("host-1", "nonce-1", good) is True
    assert bh.verify_proof("host-1", "nonce-1", "deadbeef") is False
    assert bh.verify_proof("host-1", "other-nonce", good) is False
    assert bh.verify_proof("unknown", "nonce-1", good) is False
    assert bh.verify_proof("host-1", "", good) is False
    assert bh.verify_proof("host-1", "nonce-1", "") is False


def test_verify_proof_fails_closed_when_the_secret_is_unreadable():
    bh.enroll_host("owner", "host-1", secret="s3cret-value", now=T)
    proof = bh.proof_for("s3cret-value", "host-1", "n")
    assert bh.verify_proof("host-1", "n", proof) is True
    # A rotated key makes the sealed blob unreadable -> the proof can't verify.
    os.environ["WEB_SESSION_SECRET"] = "rotated-key"
    assert bh.verify_proof("host-1", "n", proof) is False


# ── 3. liveness ───────────────────────────────────────────────────────

def test_heartbeat_keeps_a_host_available():
    bh.enroll_host("owner", "host-1", now=T)
    bh.mark_online("host-1", now=T)
    assert bh.get_host("host-1", now=T).is_available(T) is True
    assert bh.get_host("host-1", now=T).effective_state(T) == bh.STATE_ONLINE


def test_stale_heartbeat_sweeps_to_unavailable_and_is_idempotent():
    bh.enroll_host("owner", "host-1", now=T)
    bh.mark_online("host-1", now=T)
    timeout = bh.heartbeat_timeout()
    # A fresh beat within the window: nothing swept.
    assert bh.sweep_stale(now=T + timeout - 1) == []
    swept = bh.sweep_stale(now=T + timeout)
    assert len(swept) == 1 and swept[0]["state"] == bh.STATE_UNAVAILABLE
    assert swept[0]["available"] is False
    assert bh.sweep_stale(now=T + timeout + 1) == []            # idempotent
    # A new heartbeat restores online immediately.
    restored = bh.heartbeat("host-1", now=T + timeout + 2)
    assert restored["state"] == bh.STATE_ONLINE


def test_effective_state_reports_unavailable_past_deadline():
    bh.enroll_host("owner", "host-1", now=T)
    bh.mark_online("host-1", now=T)
    host = bh.get_host("host-1")
    assert host.effective_state(T + bh.heartbeat_timeout()) == bh.STATE_UNAVAILABLE
    assert host.is_available(T + bh.heartbeat_timeout()) is False


def test_never_connected_host_is_offline_and_unavailable():
    bh.enroll_host("owner", "host-1", now=T)
    host = bh.get_host("host-1")
    assert host.state == bh.STATE_OFFLINE
    assert host.is_available(T) is False


# ── 4. owner isolation / bindings ─────────────────────────────────────

def test_list_hosts_is_owner_scoped():
    bh.enroll_host("alice", "a1", now=T)
    bh.enroll_host("alice", "a2", now=T)
    bh.enroll_host("bob", "b1", now=T)
    assert {h["host_id"] for h in bh.list_hosts("alice")} == {"a1", "a2"}
    assert {h["host_id"] for h in bh.list_hosts("bob")} == {"b1"}
    assert bh.list_hosts("nobody") == []


def test_bind_and_route_by_bot():
    bh.enroll_host("owner", "host-1", now=T)
    bh.enroll_host("owner", "host-2", now=T)
    bh.bind_bot("owner", "bot-a", "host-1", now=T)
    bh.bind_bot("owner", "bot-b", "host-2", now=T)
    assert bh.host_for("owner", "bot-a").host_id == "host-1"
    assert bh.host_for("owner", "bot-b").host_id == "host-2"
    assert bh.binding_for("owner", "bot-a") == "host-1"
    assert bh.unbind_bot("owner", "bot-a") is True
    assert bh.host_for("owner", "bot-a") is None                # no binding, 2 hosts


def test_bind_rejects_a_foreign_or_unknown_host():
    bh.enroll_host("alice", "host-1", now=T)
    with pytest.raises(bh.HostError):
        bh.bind_bot("bob", "bot", "host-1", now=T)              # foreign
    with pytest.raises(bh.HostError):
        bh.bind_bot("alice", "bot", "nope", now=T)              # unknown


def test_host_for_single_host_owner_needs_no_binding():
    bh.enroll_host("owner", "only", now=T)
    assert bh.host_for("owner").host_id == "only"


def test_host_for_is_ambiguous_with_two_hosts_and_no_binding():
    bh.enroll_host("owner", "h1", now=T)
    bh.enroll_host("owner", "h2", now=T)
    assert bh.host_for("owner") is None                         # never guesses


# ── 5. redaction ──────────────────────────────────────────────────────

def test_cdp_urls_and_secrets_are_redacted():
    text = ("cdp ws://127.0.0.1:9222/devtools/browser/abc "
            "http://127.0.0.1:9222/json/version secret=topsecret")
    out = bh.redact_text(text)
    assert "devtools/browser" not in out
    assert "/json/version" not in out
    assert "topsecret" not in out
    assert "[redacted-cdp]" in out


def test_public_view_never_carries_the_secret():
    out = bh.enroll_host("owner", "host-1", now=T)
    view = bh.public_view(bh.get_host("host-1"), now=T)
    assert "sealed" not in view and "secret" not in view
    assert out["secret"] not in json.dumps(view)


# ── 6. fail closed ────────────────────────────────────────────────────

def test_enroll_fails_closed_without_a_key(monkeypatch):
    monkeypatch.delenv("WEB_SESSION_SECRET", raising=False)
    monkeypatch.delenv("KYREX_PROVIDER_SECRETS_KEY", raising=False)
    with pytest.raises(bh.HostError):
        bh.enroll_host("owner", "host-1", now=T)
    assert not bh._registry_path().exists()                    # nothing written
