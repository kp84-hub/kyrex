"""Run with python3 -m unittest kyrex-cloud/test_level6_message_schedule.py."""
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import level6_message_schedule as schedule  # noqa: E402
from task_store import DuplicateTaskId  # noqa: E402


class ScheduleTests(unittest.TestCase):
    def test_sunday_seven_eastern_and_dst(self):
        eastern = ZoneInfo("America/New_York")
        self.assertIsNone(schedule.due_date(datetime(2026, 9, 27, 18, 59, tzinfo=eastern)))
        self.assertEqual(schedule.due_date(datetime(2026, 9, 27, 23, 0, tzinfo=timezone.utc)),
                         "2026-09-27")  # Sunday 7 PM EDT
        self.assertEqual(schedule.due_date(datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)),
                         None)  # Still Saturday Eastern
        self.assertEqual(schedule.due_date(datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)),
                         "2026-11-08")  # Sunday 7 PM EST
        self.assertIsNone(schedule.due_date(datetime(2026, 9, 27, 20, 0, tzinfo=eastern)))

    def test_exactly_one_running_bound_calendar_bot_then_durable_once(self):
        bot = {"id": "calendar", "owner": "alice", "status": "running", "rift": "/tmp/cal"}
        candidates = [bot]
        bindings = {"calendar": "host-1"}
        fake_bots = types.SimpleNamespace(load_bots=lambda: {b["id"]: b for b in candidates})
        fake_hosts = types.SimpleNamespace(binding_for=lambda owner, bid: bindings.get(bid, ""))
        fake_serve = types.SimpleNamespace(calendar_bot_granted=lambda b: True,
                                           LEVEL6_MESSAGE_REQUEST="send-calendar")
        submitted = set()
        calls = []

        class Store:
            def submit(self, **kwargs):
                calls.append(kwargs)
                if kwargs["task_id"] in submitted:
                    raise DuplicateTaskId("duplicate")
                submitted.add(kwargs["task_id"])

        with patch.dict(sys.modules, {"bots": fake_bots, "browser_hosts": fake_hosts,
                                     "serve": fake_serve}):
            now = datetime(2026, 9, 27, 19, 1, tzinfo=ZoneInfo("America/New_York"))
            self.assertEqual(schedule.submit_due(Store(), now=now, owner="alice"), "queued")
            self.assertEqual(schedule.submit_due(Store(), now=now, owner="alice"), "already queued")
            self.assertEqual(len(submitted), 1)
            self.assertEqual(calls[0]["task_text"], "send-calendar")
            self.assertEqual(calls[0]["bot_id"], "calendar")
            self.assertEqual(calls[0]["chat_id"], "alice")
            candidates.append({"id": "calendar-two", "owner": "alice", "status": "running"})
            bindings["calendar-two"] = "host-1"
            self.assertEqual(schedule.submit_due(Store(), now=now, owner="alice"),
                             "Calendar Bot binding unavailable")
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
