"""Regression: a long calendar agenda must keep its first day in Chat."""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import connectors  # noqa: E402
import serve  # noqa: E402
import task_store  # noqa: E402


class CalendarResultRelayTests(unittest.TestCase):
    def test_calendar_task_keeps_first_and_last_days_after_result_formatting(self):
        days = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01",
                "2026-10-02", "2026-10-03", "2026-10-04"]
        events = [
            {"summary": f"Dummy event {number}: " + "Practice " * 9,
             "start": {"date": day}, "end": {"date": next_day}}
            for number, (day, next_day) in enumerate(zip(
                days, days[1:] + ["2026-10-05"]), 1)
        ]
        owner = "alice"
        reader = types.SimpleNamespace(events=lambda **kwargs: events)
        connector = types.SimpleNamespace(
            calendar=lambda identity: reader,
            preferred_calendar=lambda identity: "primary")
        task = types.SimpleNamespace(get=lambda task_id: {
            "status": task_store.STATUS_RUNNING, "cancel_requested": False})
        ctx = types.SimpleNamespace(bot_id="calendar", bot_owner=owner,
                                    policy={"cal:list": 0})
        results = []
        relays = []
        with (patch.object(task_store, "CloudTaskStore", return_value=task),
              patch.object(connectors, "default_store", return_value=connector),
              patch.object(serve.audit, "log")):
            serve._run_calendar_read_task(
                ctx, owner, serve.CALENDAR_TASK_WEEK, "task-1",
                lambda chat_id, text: relays.append(text),
                on_result=results.append)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["mode"], "calendar")
        self.assertGreater(len(results[0]["final_response"]), 600)
        displayed = serve.format_result(results[0])
        self.assertEqual(displayed, results[0]["final_response"])
        self.assertEqual(relays, [displayed])
        self.assertIn("**Monday, Sep 28**", displayed)
        self.assertIn("**Sunday, Oct 04**", displayed)
        self.assertLess(displayed.index("Monday"), displayed.index("Tuesday"))

    def test_explicit_date_reads_fair_from_one_owner_calendar_day(self):
        calls = []
        event = {"summary": "Fair", "start": {"date": "2026-10-16"},
                 "end": {"date": "2026-10-17"}}
        def events(**kwargs):
            calls.append(kwargs)
            return [event]
        reader = types.SimpleNamespace(events=events)
        connector = types.SimpleNamespace(calendar=lambda identity: reader,
                                          preferred_calendar=lambda identity: "primary")
        task = types.SimpleNamespace(get=lambda task_id: {
            "status": task_store.STATUS_RUNNING, "cancel_requested": False})
        ctx = types.SimpleNamespace(bot_id="calendar", bot_owner="alice", policy={"cal:list": 0})
        results = []
        with (patch.object(task_store, "CloudTaskStore", return_value=task),
              patch.object(connectors, "default_store", return_value=connector),
              patch.object(serve.audit, "log")):
            serve._run_calendar_read_task(ctx, "alice", "calendar: 2026-10-16", "t1",
                                          lambda *args: None, on_result=results.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["time_min"], "2026-10-16T00:00:00-04:00")
        self.assertEqual(calls[0]["time_max"], "2026-10-17T00:00:00-04:00")
        self.assertEqual(calls[0]["calendar_id"], "primary")
        self.assertIn("Friday, Oct 16, 2026", results[0]["final_response"])
        self.assertIn("All day", results[0]["final_response"])
        self.assertIn("Fair", results[0]["final_response"])

    def test_keyword_search_returns_matching_event_dates_and_times(self):
        calls = []
        events = [{"summary": "Stella heartworm medication", "start": {
            "dateTime": "2026-10-16T08:30:00-04:00"}, "end": {
            "dateTime": "2026-10-16T08:45:00-04:00"}}]
        def query_events(**kwargs):
            calls.append(kwargs)
            return events
        reader = types.SimpleNamespace(events=query_events)
        connector = types.SimpleNamespace(
            calendar=lambda identity: reader,
            preferred_calendar=lambda identity: "primary")
        task = types.SimpleNamespace(get=lambda task_id: {
            "status": task_store.STATUS_RUNNING, "cancel_requested": False})
        ctx = types.SimpleNamespace(bot_id="calendar", bot_owner="alice",
                                    policy={"cal:list": 0})
        results = []
        with (patch.object(task_store, "CloudTaskStore", return_value=task),
              patch.object(connectors, "default_store", return_value=connector),
              patch.object(serve.audit, "log")):
            serve._run_calendar_read_task(
                ctx, "alice", "calendar: search Stella heartworm meds", "t2",
                lambda *args: None, on_result=results.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["query"], "Stella heartworm meds")
        self.assertEqual(calls[0]["calendar_id"], "primary")
        self.assertTrue(calls[0]["require_complete"])
        self.assertIn("Oct 16", results[0]["final_response"])
        self.assertIn("8:30 AM–8:45 AM", results[0]["final_response"])
        self.assertIn("Stella heartworm medication", results[0]["final_response"])

    def test_other_conversational_results_keep_their_existing_bound(self):
        long_text = "first day " + "x" * 700
        self.assertEqual(serve.format_result({
            "status": "no_changes", "final_response": long_text}),
            long_text[-600:])


if __name__ == "__main__":
    unittest.main()
