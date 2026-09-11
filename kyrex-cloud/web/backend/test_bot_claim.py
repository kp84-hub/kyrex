"""One-time legacy Bot ownership claim.

Legacy Bots were persisted before ownership existed, so they carry an empty
``owner``. They are VISIBLE to signed-in users but NOT manageable, because the
lifecycle/configuration endpoints require ``bot.owner == user``. This suite
proves the single, safe mutation that fixes that:

  POST /api/bots/{id}/claim  — assign the caller as owner of an OWNERLESS Bot.

Coverage:
  1.  registry claim assigns owner and preserves every other field.
  2.  registry claim refuses an already-owned Bot (BotAlreadyOwned), an unknown
      Bot (KeyError), and an empty owner (ValueError).
  3.  the configured Kyrex web operator can claim an ownerless Bot; the Bot
      then becomes manageable (lifecycle + configure controls apply).
  4.  a non-operator (authenticated, but not the operator) gets 403, and an
      anonymous caller gets 401 — neither changes anything.
  5.  an already-owned Bot (another user's, or the operator's own) gets 409 and
      its owner is never overwritten.
  6.  claiming mutates ONLY ``owner`` — status, policy, rift, model and system
      prompt are untouched (it does not start the Bot or change its policy).
  7.  owner isolation: after a claim the Bot is visible/manageable to the
      operator alone, and gone for everyone else.
  8.  claiming an unknown Bot is a 404.
  9.  claiming is ONE-TIME: a second claim is refused and inert, and N
      concurrent claims on one ownerless Bot produce exactly one winner.

Run: python3 -m pytest test_bot_claim.py
"""

import os
import subprocess
import sys
import tempfile

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-bot-claim-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main  # noqa: E402  (after env setup; seeds the shared app/session map)
import chat_service  # noqa: E402
import bots  # noqa: E402  — the authoritative registry under test


# ── helpers ────────────────────────────────────────────────────────

def _reset():
    """Fresh chat store + fresh Bot registry."""
    root = chat_service._chat_root()
    for p in root.rglob("*.json"):
        p.unlink()
    for p in root.rglob("*.json.tmp"):
        p.unlink()
    bots.save_bots({})


def setup_function():
    _reset()
    # The operator (the configured allowed GitHub username) plus two ordinary,
    # authenticated non-operators.
    main.sessions["sess-op"] = main.ALLOWED_USERNAME
    main.sessions["sess-alice"] = "alice"
    main.sessions["sess-bob"] = "bob"


def teardown_function():
    _reset()
    main.sessions.pop("sess-op", None)


def _git_rift() -> str:
    """A real git repository workspace — a valid Developer-Bot Rift."""
    d = tempfile.mkdtemp(prefix="kyrex-claim-git-rift-")
    subprocess.run(["git", "init", "-q", d], check=True,
                   capture_output=True, text=True)
    return d


def _plain_rift() -> str:
    return tempfile.mkdtemp(prefix="kyrex-claim-rift-")


def _bot(bot_id="legacy", owner="", status="stopped", rift=None,
         policy=None, system_prompt="", repo=""):
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "anthropic:claude-test",
        rift or _plain_rift(), policy=policy, status=status, owner=owner,
        system_prompt=system_prompt, repo=repo,
    )


def _client(user="alice"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


def _operator_client():
    return _client("op")


# ── 1. registry-level claim ───────────────────────────────────────

def test_registry_claim_assigns_owner_and_preserves_other_fields():
    bot = _bot("legacy", owner="")
    updated = bots.claim_bot("legacy", "op")
    assert updated["owner"] == "op"
    # Everything else is byte-for-byte identical.
    reloaded = bots.get_bot("legacy")
    for key, value in bot.items():
        if key == "owner":
            continue
        assert reloaded[key] == value, key
    assert reloaded["owner"] == "op"


def test_registry_claim_never_steals_and_rejects_bad_input():
    _bot("owned", owner="bob")
    with pytest.raises(bots.BotAlreadyOwned):
        bots.claim_bot("owned", "op")
    assert bots.get_bot("owned")["owner"] == "bob"   # untouched

    _bot("legacy", owner="")
    with pytest.raises(KeyError):
        bots.claim_bot("ghost", "op")
    with pytest.raises(ValueError):
        bots.claim_bot("legacy", "")
    assert bots.get_bot("legacy")["owner"] == ""     # untouched


# ── 2. operator claims via the API ────────────────────────────────

def test_operator_claims_ownerless_bot_and_gains_management():
    _bot("legacy", owner="", status="stopped", rift=_git_rift())

    # Before: visible to the operator, but NOT manageable.
    before = {b["id"]: b for b in _operator_client().get("/api/bots").json()["bots"]}
    assert before["legacy"]["manageable"] is False
    assert before["legacy"]["claimable"] is True
    # ...and the lifecycle endpoint refuses it (owner-scoped rule unchanged).
    assert _operator_client().patch(
        "/api/bots/legacy", json={"status": "running"}).status_code == 403

    r = _operator_client().post("/api/bots/legacy/claim", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "legacy"
    assert body["manageable"] is True
    assert body["claimable"] is False
    assert bots.get_bot("legacy")["owner"] == main.ALLOWED_USERNAME

    # After: the EXISTING owner-scoped controls now apply unchanged.
    started = _operator_client().patch(
        "/api/bots/legacy", json={"status": "running"})
    assert started.status_code == 200, started.text
    configured = _operator_client().post(
        "/api/bots/legacy/configure", json={"preset": "developer"})
    assert configured.status_code == 200, configured.text


# ── 3. non-operator / anonymous denial ────────────────────────────

def test_non_operator_and_anonymous_cannot_claim():
    _bot("legacy", owner="")

    assert _client("alice").post(
        "/api/bots/legacy/claim", json={}).status_code == 403

    from fastapi.testclient import TestClient
    assert TestClient(main.app).post(
        "/api/bots/legacy/claim", json={}).status_code == 401

    assert bots.get_bot("legacy")["owner"] == ""     # nothing changed


# ── 4. already-owned denial ───────────────────────────────────────

def test_cannot_claim_a_bot_that_already_has_an_owner():
    _bot("bobs", owner="bob")            # another user's Bot
    _bot("mine", owner=main.ALLOWED_USERNAME)  # already owned by the operator

    assert _operator_client().post(
        "/api/bots/bobs/claim", json={}).status_code == 409
    assert _operator_client().post(
        "/api/bots/mine/claim", json={}).status_code == 409

    assert bots.get_bot("bobs")["owner"] == "bob"
    assert bots.get_bot("mine")["owner"] == main.ALLOWED_USERNAME


def test_claim_unknown_bot_is_404():
    assert _operator_client().post(
        "/api/bots/ghost/claim", json={}).status_code == 404


def test_second_claim_of_the_same_bot_fails_safely():
    """A claim is one-time: once owned, re-claiming is refused and inert."""
    _bot("legacy", owner="")

    first = _operator_client().post("/api/bots/legacy/claim", json={})
    assert first.status_code == 200, first.text
    owner_after_first = bots.get_bot("legacy")["owner"]
    assert owner_after_first == main.ALLOWED_USERNAME

    # Second claim (same operator, and a non-operator) is refused...
    second = _operator_client().post("/api/bots/legacy/claim", json={})
    assert second.status_code == 409, second.text
    assert _client("alice").post(
        "/api/bots/legacy/claim", json={}).status_code == 403

    # ...and changes nothing.
    assert bots.get_bot("legacy")["owner"] == owner_after_first


def test_concurrent_claims_exactly_one_wins():
    """Atomicity: N racing claims on ONE ownerless Bot yield one winner."""
    import threading

    _bot("race", owner="")
    claimants = [f"user-{i}" for i in range(12)]
    results: dict[str, str] = {}
    lock = threading.Lock()
    barrier = threading.Barrier(len(claimants))

    def attempt(name):
        barrier.wait()  # maximise overlap
        try:
            bots.claim_bot("race", name)
            outcome = "won"
        except bots.BotAlreadyOwned:
            outcome = "lost"
        with lock:
            results[name] = outcome

    threads = [threading.Thread(target=attempt, args=(c,)) for c in claimants]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [name for name, outcome in results.items() if outcome == "won"]
    assert len(winners) == 1, results
    assert set(results.values()) == {"won", "lost"}
    # The registry reflects exactly the single winner — no interleaving.
    assert bots.get_bot("race")["owner"] == winners[0]


# ── 5. claim mutates NOTHING but owner ────────────────────────────

def test_claim_does_not_mutate_status_policy_or_rift():
    rift = _plain_rift()
    _bot("legacy", owner="", status="paused", rift=rift,
         policy={"fs:read": 0}, system_prompt="be careful", repo="o/r")
    snapshot = dict(bots.get_bot("legacy"))

    assert _operator_client().post(
        "/api/bots/legacy/claim", json={}).status_code == 200

    after = dict(bots.get_bot("legacy"))
    # Owner is the ONLY change.
    assert after.pop("owner") == main.ALLOWED_USERNAME
    assert snapshot.pop("owner") == ""
    assert after == snapshot
    # Spelled out, because these are the requirements in plain terms.
    assert after["status"] == "paused"          # not started
    assert after["policy"] == {"fs:read": 0}    # policy unchanged
    assert after["rift"] == rift
    assert after["system_prompt"] == "be careful"
    assert after["model"] == "anthropic:claude-test"


# ── 6. owner isolation after a claim ──────────────────────────────

def test_claimed_bot_is_isolated_to_the_operator():
    _bot("legacy", owner="")

    # Ownerless → visible to anyone signed in (so they see it is claimable).
    bob_before = {b["id"] for b in _client("bob").get("/api/bots").json()["bots"]}
    assert "legacy" in bob_before

    assert _operator_client().post(
        "/api/bots/legacy/claim", json={}).status_code == 200

    # Now owned by the operator: invisible and unmanageable to everyone else.
    bob_after = {b["id"] for b in _client("bob").get("/api/bots").json()["bots"]}
    assert "legacy" not in bob_after
    assert _client("bob").patch(
        "/api/bots/legacy", json={"status": "running"}).status_code == 403
    assert _client("bob").post(
        "/api/bots/legacy/configure", json={"preset": "developer"}
    ).status_code == 403

    # ...still visible and manageable to the operator.
    op_after = {b["id"]: b for b in _operator_client().get("/api/bots").json()["bots"]}
    assert op_after["legacy"]["manageable"] is True
    assert op_after["legacy"]["claimable"] is False
    assert bots.get_bot("legacy")["owner"] == main.ALLOWED_USERNAME
