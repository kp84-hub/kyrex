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
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["query"], "Stella heartworm meds")
        self.assertEqual(calls[1]["query"], "Stella heart warm meds")
        self.assertEqual(calls[0]["calendar_id"], "primary")
        self.assertTrue(calls[0]["require_complete"])
        self.assertIn("· 1 event", results[0]["final_response"])
        self.assertIn("Oct 16", results[0]["final_response"])
        self.assertIn("8:30 AM–8:45 AM", results[0]["final_response"])
        self.assertIn("Stella heartworm medication", results[0]["final_response"])

    def test_split_and_joined_heartworm_spellings_merge_without_duplicates(self):
        calls = []
        events_by_query = {
            "Stella heart warm meds": [{"id": "old", "summary":
                "Heart Warm Meds for Stella", "start": {"dateTime":
                "2026-08-24T08:00:00-04:00"}, "end": {"dateTime":
                "2026-08-24T09:00:00-04:00"}}],
            "Stella heartworm meds": [
                {"id": "old", "summary": "Heart Warm Meds for Stella",
                 "start": {"dateTime": "2026-08-24T08:00:00-04:00"},
                 "end": {"dateTime": "2026-08-24T09:00:00-04:00"}},
                {"id": "oct", "summary": "Stella heartworm meds",
                 "start": {"date": "2026-10-24"},
                 "end": {"date": "2026-10-25"}},
                {"id": "nov", "summary": "Stella heartworm meds",
                 "start": {"date": "2026-11-22"},
                 "end": {"date": "2026-11-23"}},
            ],
        }

        def query_events(**kwargs):
            calls.append(kwargs)
            return events_by_query[kwargs["query"]]

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
                ctx, "alice", "calendar: search Stella heart warm meds", "t3",
                lambda *args: None, on_result=results.append)
        self.assertEqual([c["query"] for c in calls], [
            "Stella heart warm meds", "Stella heartworm meds"])
        self.assertEqual(len(results), 1)
        output = results[0]["final_response"]
        self.assertIn("· 3 events", output)
        self.assertIn("Monday, Aug 24", output)
        self.assertIn("Saturday, Oct 24", output)
        self.assertIn("Sunday, Nov 22", output)
        self.assertEqual(output.count("Heart Warm Meds for Stella"), 1)
        self.assertEqual(output.count("Stella heartworm meds"), 2)

    def test_empty_exact_search_falls_back_without_confusing_medications(self):
        calls = []
        candidates = [
            {"id": "good", "summary": "Heartworm medication for Stella",
             "start": {"date": "2026-10-24"}, "end": {"date": "2026-10-25"}},
            {"id": "flea", "summary": "Stella flea meds",
             "start": {"date": "2026-10-24"}, "end": {"date": "2026-10-25"}},
            {"id": "birthday", "summary": "Stella birthday",
             "start": {"date": "2026-10-24"}, "end": {"date": "2026-10-25"}},
        ]
        def query_events(**kwargs):
            calls.append(kwargs)
            return candidates if kwargs["query"] == "stella heartworm" else []
        connector = types.SimpleNamespace(
            calendar=lambda owner: types.SimpleNamespace(events=query_events),
            preferred_calendar=lambda owner: "chosen-calendar")
        task = types.SimpleNamespace(get=lambda task_id: {
            "status": task_store.STATUS_RUNNING, "cancel_requested": False})
        ctx = types.SimpleNamespace(bot_id="calendar", bot_owner="alice",
                                    policy={"cal:list": 0})
        results = []
        with (patch.object(task_store, "CloudTaskStore", return_value=task),
              patch.object(connectors, "default_store", return_value=connector),
              patch.object(serve.audit, "log")):
            serve._run_calendar_read_task(
                ctx, "alice", "calendar: search Stella heartworm meds", "t4",
                lambda *args: None, on_result=results.append)
        self.assertEqual([call["query"] for call in calls], [
            "Stella heartworm meds", "Stella heart warm meds", "stella heartworm"])
        self.assertTrue(all(c["require_complete"] for c in calls))
        self.assertTrue(all(c["calendar_id"] == "chosen-calendar" for c in calls))
        output = results[0]["final_response"]
        self.assertIn("Heartworm medication for Stella", output)
        self.assertNotIn("birthday", output)
        self.assertNotIn("flea meds", output)
        self.assertIn("Other calendars were not checked", output)

    def test_negative_search_reports_scope_and_incomplete_search_is_not_empty(self):
        for incomplete in (False, True):
            def query_events(**kwargs):
                if incomplete and kwargs["query"] == "stella flea":
                    raise connectors.ConnectorUnavailable("calendar lookup was incomplete")
                return []
            connector = types.SimpleNamespace(
                calendar=lambda owner: types.SimpleNamespace(events=query_events),
                preferred_calendar=lambda owner: "primary")
            task = types.SimpleNamespace(get=lambda task_id: {
                "status": task_store.STATUS_RUNNING, "cancel_requested": False})
            ctx = types.SimpleNamespace(bot_id="calendar", bot_owner="alice",
                                        policy={"cal:list": 0})
            results, relays = [], []
            with (patch.object(task_store, "CloudTaskStore", return_value=task),
                  patch.object(connectors, "default_store", return_value=connector),
                  patch.object(serve.audit, "log")):
                serve._run_calendar_read_task(
                    ctx, "alice", "calendar: search Stella flea meds", "t5",
                    lambda cid, text: relays.append(text), on_result=results.append)
            output = results[0]["final_response"]
            if incomplete:
                self.assertIn("unavailable", output)
                self.assertNotIn("No matching", output)
            else:
                self.assertIn("No matching calendar events", output)
                self.assertIn("Searched your selected calendar from", output)
                self.assertIn("Other calendars were not checked", output)

    def test_medication_aliases_and_possessives_preserve_subject(self):
        self.assertEqual(serve.calendar_search_fallback_query("Stella’s flea pills"),
                         "stella flea")
        self.assertTrue(serve.calendar_search_fallback_matches(
            "Stella's flea pills", {"summary": "Flea medicine for Stella"}))
        self.assertFalse(serve.calendar_search_fallback_matches(
            "Stella heartworm meds", {"summary": "Stella flea medication"}))
        self.assertIsNone(serve.calendar_search_fallback_query("meds"))
        self.assertIsNone(serve.calendar_search_fallback_query("dentist"))

    def test_other_conversational_results_keep_their_existing_bound(self):
        long_text = "first day " + "x" * 700
        self.assertEqual(serve.format_result({
            "status": "no_changes", "final_response": long_text}),
            long_text[-600:])


if __name__ == "__main__":
    unittest.main()
