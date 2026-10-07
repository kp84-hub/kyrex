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
    def test_sunday_evening_upcoming_week_and_dst(self):
        eastern = ZoneInfo("America/New_York")
        self.assertIsNone(schedule.due_date(datetime(2026, 10, 11, 18, 59, tzinfo=eastern)))
        self.assertEqual(schedule.due_date(datetime(2026, 10, 11, 23, 0, tzinfo=timezone.utc)),
                         "2026-10-12")  # Sunday 7 PM EDT
        self.assertEqual(schedule.due_date(datetime(2026, 10, 11, 19, 59, tzinfo=eastern)),
                         "2026-10-12")  # Restart within the due hour
        self.assertIsNone(schedule.due_date(datetime(2026, 10, 11, 20, 0, tzinfo=eastern)))
        self.assertIsNone(schedule.due_date(datetime(2026, 10, 12, 7, 0, tzinfo=eastern)))
        self.assertEqual(schedule.due_date(datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)),
                         "2026-11-02")  # Sunday 7 PM EST after DST ends

    def test_exactly_one_running_bound_calendar_bot_then_durable_once(self):
        bot = {"id": "calendar", "owner": "alice", "status": "running",
               "rift": "/tmp/cal", "role": "calendar"}
        browser = {"id": "browser-bot", "owner": "alice", "status": "running",
                   "role": "browser", "policy": {"role": "browser"}}
        candidates = [bot, browser]
        bindings = {"calendar": "host-1", "browser-bot": "host-1"}
        fake_bots = types.SimpleNamespace(load_bots=lambda: {b["id"]: b for b in candidates})
        fake_hosts = types.SimpleNamespace(binding_for=lambda owner, bid: bindings.get(bid, ""))
        fake_serve = types.SimpleNamespace(
            calendar_bot_granted=lambda b: b.get("role") == "calendar",
            is_browser_bot_policy=lambda b: isinstance(b, dict) and b.get("role") == "browser",
            LEVEL6_MESSAGE_PREVIEW_REQUEST="preview-facebook-weekly")
        fake_weekly = types.SimpleNamespace(BROWSER_BOT_ID="browser-bot")
        submitted = set()
        calls = []

        class Store:
            def submit(self, **kwargs):
                calls.append(kwargs)
                if kwargs["task_id"] in submitted:
                    raise DuplicateTaskId("duplicate")
                submitted.add(kwargs["task_id"])

        with patch.dict(sys.modules, {"bots": fake_bots, "browser_hosts": fake_hosts,
                                     "serve": fake_serve, "level6_weekly": fake_weekly}), \
                patch.object(schedule, "preview_conversation", return_value="preview-chat"):
            now = datetime(2026, 10, 11, 19, 1, tzinfo=ZoneInfo("America/New_York"))
            self.assertEqual(schedule.submit_due(Store(), now=now, owner="alice"), "queued")
            self.assertEqual(schedule.submit_due(Store(), now=now, owner="alice"), "already queued")
            self.assertEqual(len(submitted), 1)
            self.assertEqual(calls[0]["task_text"], "preview-facebook-weekly")
            self.assertEqual(calls[0]["bot_id"], "calendar")
            self.assertEqual(calls[0]["chat_id"], "alice")
            self.assertEqual(calls[0]["conversation_id"], "preview-chat")
            self.assertEqual(schedule.expected_week(calls[0]["task_id"], "alice"), "2026-10-12")
            self.assertIsNone(schedule.expected_week(calls[0]["task_id"], "bob"))
            candidates.append({"id": "calendar-two", "owner": "alice", "status": "running",
                               "role": "calendar"})
            bindings["calendar-two"] = "host-1"
            self.assertEqual(schedule.submit_due(Store(), now=now, owner="alice"),
                             "Browser Bot or Calendar Bot binding unavailable")
            self.assertEqual(len(calls), 2)
            candidates.pop()
            bindings["browser-bot"] = "other-host"
            self.assertEqual(schedule.submit_due(Store(), now=now, owner="alice"),
                             "Browser Bot or Calendar Bot binding unavailable")

    def test_preview_opt_in_does_not_require_legacy_send_switch(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(schedule.enabled())
            with patch.dict("os.environ", {"KYREX_LEVEL6_PREVIEW_SCHEDULE_ENABLED": "1",
                                           "KYREX_LEVEL6_SEND_ENABLED": "0"}):
                self.assertTrue(schedule.enabled())
            with patch.dict("os.environ", {"KYREX_LEVEL6_SCHEDULE_ENABLED": "1"}):
                self.assertTrue(schedule.enabled())
                with patch.dict("os.environ", {"KYREX_LEVEL6_PREVIEW_SCHEDULE_ENABLED": "0"}):
                    self.assertFalse(schedule.enabled())


if __name__ == "__main__":
    unittest.main()
