"""Restart, initial baseline, and ambiguous submission recovery tests."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from email_watcher import CloudClient, Watcher

RULE = {"id": "school", "version": "v1"}


class FakeCloud:
    def __init__(self):
        self.ids = {"old"}
        self.tasks = {}
        self.attempts = 0
        self.lose_response = False

    def snapshot(self, rule):
        return set(self.ids)

    def submit(self, rule, mid):
        self.attempts += 1
        self.tasks.setdefault(mid, "task-" + mid)
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError()
        return self.tasks[mid]


def test_baseline_then_deliver_new_email_and_preserve_receipts_after_restart(tmp_path):
    cloud = FakeCloud()
    path = tmp_path / "state.sqlite3"
    watcher = Watcher(cloud, path)
    assert watcher.poll_rule(RULE) == {"baseline": True, "queued": 0}
    assert cloud.tasks == {}
    cloud.ids.add("new")
    assert watcher.poll_rule(RULE) == {"baseline": False, "queued": 1}
    watcher.close()
    restarted = Watcher(cloud, path)
    assert restarted.poll_rule(RULE) == {"baseline": False, "queued": 0}
    assert cloud.attempts == 1
    restarted.close()


def test_lost_response_retries_same_event_without_losing_it(tmp_path):
    cloud = FakeCloud()
    watcher = Watcher(cloud, tmp_path / "state.sqlite3")
    watcher.poll_rule(RULE)
    cloud.ids.add("new")
    cloud.lose_response = True
    with pytest.raises(TimeoutError):
        watcher.poll_rule(RULE)
    watcher.close()
    watcher = Watcher(cloud, tmp_path / "state.sqlite3")
    assert watcher.poll_rule(RULE)["queued"] == 1
    assert cloud.tasks == {"new": "task-new"}
    assert cloud.attempts == 2
    watcher.close()


def test_changed_rule_gets_quiet_baseline(tmp_path):
    cloud = FakeCloud()
    watcher = Watcher(cloud, tmp_path / "state.sqlite3")
    watcher.poll_rule(RULE)
    cloud.ids.add("new")
    assert watcher.poll_rule({**RULE, "version": "v2"})["baseline"]
    assert cloud.tasks == {}
    watcher.close()


def test_failed_snapshot_cannot_create_partial_baseline(tmp_path):
    cloud = FakeCloud()
    def broken(rule):
        raise ValueError("incomplete pagination")
    cloud.snapshot = broken
    watcher = Watcher(cloud, tmp_path / "state.sqlite3")
    with pytest.raises(ValueError):
        watcher.poll_rule(RULE)
    assert watcher.db.execute("SELECT count(*) FROM baselines").fetchone()[0] == 0
    watcher.close()


@pytest.mark.parametrize("url", ["http://chat.kyrex.dev", "https://user:pass@chat.kyrex.dev",
                                 "https://chat.kyrex.dev/evil", "https://chat.kyrex.dev?q=x"])
def test_service_credential_requires_https_origin(url):
    with pytest.raises(ValueError):
        CloudClient(url, "x" * 40)


def test_pagination_exhaustion_is_error_not_partial_snapshot():
    client = CloudClient("https://chat.kyrex.dev", "x" * 40)
    calls = []
    def request(path):
        calls.append(path)
        return {"message_ids": [str(len(calls))], "next_page_token": str(len(calls))}
    client.request = request
    with pytest.raises(ValueError, match="Candidate limit"):
        client.snapshot(RULE)
    assert len(calls) == 20


def test_all_pages_are_included_before_baseline():
    client = CloudClient("https://chat.kyrex.dev", "x" * 40)
    pages = iter([{"message_ids": ["one"], "next_page_token": "next"},
                  {"message_ids": ["two"], "next_page_token": ""}])
    client.request = lambda path: next(pages)
    assert client.snapshot(RULE) == {"one", "two"}


def test_bad_message_does_not_block_other_new_messages(tmp_path):
    cloud = FakeCloud()
    watcher = Watcher(cloud, tmp_path / "state.sqlite3")
    watcher.poll_rule(RULE)
    cloud.ids.update({"bad", "good"})
    original = cloud.submit
    def submit(rule, mid):
        if mid == "bad":
            raise RuntimeError("message unavailable")
        return original(rule, mid)
    cloud.submit = submit
    with pytest.raises(RuntimeError):
        watcher.poll_rule(RULE)
    assert cloud.tasks == {"good": "task-good"}
    assert watcher.db.execute("SELECT task_id FROM receipts WHERE message_id='good'").fetchone()[0] == "task-good"
    assert watcher.db.execute("SELECT 1 FROM receipts WHERE message_id='bad'").fetchone() is None
    watcher.close()
