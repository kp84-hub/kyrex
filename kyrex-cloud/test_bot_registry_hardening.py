"""Regression tests for Bot registry hardening (bots.py).

Each test pins a defect that was previously reachable, so the OLD behavior is
gone — not merely that the code runs.

  1. Concurrent read-modify-write: two simultaneous writers must not lose one
     another's update (the registry read-modify-write is serialised).
  2. save_bots is atomic: a crash mid-write can never corrupt the live
     registry, leave a stray temp file, or drop the file mode.
  3. Malformed registry JSON fails with RegistryError — never AttributeError:
     a non-object top level ([], "hello", 5, null) and non-object Bot entries
     ({"bad": 5}, {"bad": []}, {"bad": null}).
  4. Ownership cannot be transferred through update_bot; the ownerless-only
     claim path is preserved.

Written with no pytest fixtures so it also runs standalone:
    python3 test_bot_registry_hardening.py
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bots  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────

class _Registry:
    """Point bots.BOTS_FILE at a private temp dir and restore it after."""

    def __enter__(self):
        self._saved = bots.BOTS_FILE
        self._td = tempfile.TemporaryDirectory(prefix="bot-registry-test-")
        self.dir = Path(self._td.name)
        bots.BOTS_FILE = str(self.dir / "bots.json")
        return self

    def __exit__(self, *exc):
        bots.BOTS_FILE = self._saved
        self._td.cleanup()
        return False

    @property
    def path(self) -> Path:
        return Path(bots.BOTS_FILE)

    def write_raw(self, text: str) -> None:
        self.path.write_text(text)

    def temp_leftovers(self):
        return sorted(p.name for p in self.dir.iterdir()
                      if p.name != "bots.json")

    def add(self, bot_id: str, **kw):
        kw.setdefault("name", bot_id)
        kw.setdefault("model", "test:model")
        kw.setdefault("rift", str(self.dir / bot_id))
        kw.setdefault("status", "stopped")
        return bots.add_bot(bot_id, **kw)


# ── 1. concurrent writers must not lose updates ────────────────────────

def test_concurrent_add_bot_preserves_both_bots():
    """Two simultaneous add_bot calls must BOTH be persisted.

    The interleaving window is widened deterministically (a delay inside the
    load): without the registry lock both writers read the same empty
    snapshot, both report success, and the second save silently discards the
    first Bot. With the lock the second writer's load happens only after the
    first writer's save, so both survive.
    """
    with _Registry() as reg:
        real_load = bots.load_bots

        def slow_load():
            data = real_load()
            time.sleep(0.2)  # widen the read->write window
            return data

        bots.load_bots = slow_load
        try:
            results = {}

            def writer(bid):
                try:
                    reg.add(bid)
                    results[bid] = "ok"
                except Exception as exc:  # noqa: BLE001 - reported below
                    results[bid] = f"{type(exc).__name__}: {exc}"

            threads = [threading.Thread(target=writer, args=(bid,))
                       for bid in ("alpha", "bravo")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            bots.load_bots = real_load

        assert results == {"alpha": "ok", "bravo": "ok"}, results
        stored = real_load()
        assert sorted(stored) == ["alpha", "bravo"], (
            "a concurrent write was silently lost: %r" % (sorted(stored),)
        )


def test_concurrent_writers_do_not_drop_a_status_change():
    """A status change racing another write must not be lost."""
    with _Registry() as reg:
        reg.add("alpha")
        reg.add("bravo")
        real_load = bots.load_bots

        def slow_load():
            data = real_load()
            time.sleep(0.2)
            return data

        bots.load_bots = slow_load
        try:
            errors = []

            def flip_status():
                try:
                    bots.set_status("alpha", "running")
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            def rename():
                try:
                    bots.update_bot("bravo", name="Renamed")
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=flip_status),
                       threading.Thread(target=rename)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            bots.load_bots = real_load

        assert not errors, errors
        stored = real_load()
        assert stored["alpha"]["status"] == "running"
        assert stored["bravo"]["name"] == "Renamed"


# ── 2. atomic save ─────────────────────────────────────────────────────

def test_save_bots_is_atomic_and_leaves_no_temp_file():
    """A successful save writes valid JSON and leaves no temp artifact."""
    with _Registry() as reg:
        reg.add("alpha")
        reg.add("bravo")
        # The target is valid JSON and round-trips.
        assert sorted(json.loads(reg.path.read_text())) == ["alpha", "bravo"]
        assert reg.temp_leftovers() == [], reg.temp_leftovers()


def test_save_bots_failure_never_touches_the_live_registry():
    """A crash mid-write leaves the previous registry byte-identical.

    The partial bytes land in the temp file only; the live registry is
    replaced atomically, so a torn write can never corrupt it (which would
    make load_bots fail closed and take down every Bot surface).
    """
    with _Registry() as reg:
        reg.add("alpha")
        original = reg.path.read_bytes()

        real_dump = bots.json.dump

        def exploding_dump(obj, fp, **kw):
            fp.write('{"partial": ')  # reaches the TEMP file only
            raise OSError("simulated crash mid-write")

        bots.json.dump = exploding_dump
        try:
            raised = False
            try:
                bots.save_bots({"alpha": {"id": "alpha"}})
            except OSError:
                raised = True
        finally:
            bots.json.dump = real_dump

        assert raised, "save_bots swallowed the write failure"
        assert reg.path.read_bytes() == original, "live registry was corrupted"
        assert reg.temp_leftovers() == [], (
            "temp file left behind: %r" % (reg.temp_leftovers(),)
        )
        # The registry is still loadable — no fail-closed outage.
        assert sorted(bots.load_bots()) == ["alpha"]


def test_save_bots_preserves_existing_file_mode():
    """An existing registry keeps its permissions across a save."""
    with _Registry() as reg:
        reg.add("alpha")
        os.chmod(reg.path, 0o640)
        bots.set_status("alpha", "running")
        assert (reg.path.stat().st_mode & 0o777) == 0o640, (
            "save_bots changed the registry file mode: %o"
            % (reg.path.stat().st_mode & 0o777)
        )


# ── 3. malformed data raises RegistryError, never AttributeError ───────

NON_DICT_TOP_LEVEL = ["[]", '"hello"', "5", "null", "3.5", "true"]
NON_DICT_ENTRIES = ['{"bad": 5}', '{"bad": []}', '{"bad": null}',
                    '{"bad": "x"}', '{"bad": true}']


def _assert_registry_error(payload: str, label: str) -> None:
    with _Registry() as reg:
        reg.write_raw(payload)
        try:
            loaded = bots.load_bots()
        except bots.RegistryError as exc:
            assert bots.BOTS_FILE in str(exc), (
                "%s: error does not name the file: %r" % (label, str(exc)))
            return
        except AttributeError as exc:  # the defect being pinned
            raise AssertionError(
                "%s raised AttributeError, not RegistryError: %s" % (label, exc))
        raise AssertionError(
            "%s loaded instead of failing closed: %r" % (label, loaded))


def test_non_dict_top_level_raises_registry_error():
    for payload in NON_DICT_TOP_LEVEL:
        _assert_registry_error(payload, f"top-level {payload}")


def test_non_dict_entry_raises_registry_error():
    for payload in NON_DICT_ENTRIES:
        _assert_registry_error(payload, f"entry {payload}")


def test_malformed_entry_message_names_the_offending_id():
    """A non-object entry is reported by id, not as an opaque traceback."""
    with _Registry() as reg:
        reg.write_raw('{"good": {"id": "good", "name": "G", "model": "m", '
                      '"rift": "/tmp/x", "policy": {}, "status": "stopped"}, '
                      '"bad": 5}')
        try:
            bots.load_bots()
        except bots.RegistryError as exc:
            assert "bad" in str(exc), str(exc)
        else:
            raise AssertionError("malformed entry did not raise")


def test_valid_registry_still_loads():
    """The hardening must not reject a well-formed registry."""
    with _Registry() as reg:
        reg.add("alpha")
        reg.add("bravo", status="running")
        bots.BOTS_FILE = str(reg.path)
        loaded = bots.load_bots()
        assert sorted(loaded) == ["alpha", "bravo"]
        assert bots.is_running(loaded["bravo"]) is True


# ── 4. ownership cannot be transferred ────────────────────────────────

def test_update_bot_refuses_to_change_owner():
    """update_bot must reject `owner`; ownership is not a config field."""
    with _Registry() as reg:
        reg.add("alpha", owner="alice")
        for attempt in ("attacker", "", "alice", None):
            try:
                bots.update_bot("alpha", owner=attempt)
            except ValueError as exc:
                assert "owner" in str(exc), str(exc)
            else:
                raise AssertionError(
                    "update_bot accepted owner=%r — ownership transfer" % (attempt,))
        assert bots.get_bot("alpha")["owner"] == "alice", (
            "owner was mutated despite the rejection")


def test_update_bot_still_allows_its_legitimate_fields():
    """Removing `owner` must not narrow the real configuration surface."""
    with _Registry() as reg:
        reg.add("alpha")
        updated = bots.update_bot(
            "alpha", name="New", model="openai:gpt-4o", repo="https://x/y.git",
            system_prompt="sp", policy={"fs:write": 1},
            browser_allowlist=["example.com"])
        assert updated["name"] == "New"
        assert updated["model"] == "openai:gpt-4o"
        assert updated["repo"] == "https://x/y.git"
        assert updated["system_prompt"] == "sp"
        assert updated["policy"] == {"fs:write": 1}
        assert updated["browser_allowlist"] == ["example.com"]


def test_claim_path_still_grants_ownership_once():
    """The ownerless-only claim path is preserved (and only it grants)."""
    with _Registry() as reg:
        reg.add("legacy")  # ownerless
        claimed = bots.claim_bot("legacy", "op")
        assert claimed["owner"] == "op"
        try:
            bots.claim_bot("legacy", "other")
        except bots.BotAlreadyOwned:
            pass
        else:
            raise AssertionError("a second claim won an already-owned Bot")
        assert bots.get_bot("legacy")["owner"] == "op"
        # Claiming changes ONLY the owner.
        assert bots.get_bot("legacy")["status"] == "stopped"


def test_claim_bot_does_not_override_an_existing_owner():
    with _Registry() as reg:
        reg.add("owned", owner="alice")
        try:
            bots.claim_bot("owned", "attacker")
        except bots.BotAlreadyOwned:
            pass
        else:
            raise AssertionError("claim stole an owned Bot")
        assert bots.get_bot("owned")["owner"] == "alice"


# ── standalone runner (also pytest-collectable) ────────────────────────

if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = []
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures.append(fn.__name__)
            print(f"  FAIL  {fn.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print("\n" + ("ALL TESTS PASSED" if not failures
                  else f"{len(failures)} FAILURE(S): {failures}"))
    sys.exit(1 if failures else 0)
