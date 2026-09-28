"""Focused agenda rendering checks; no real calendar data is used."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import calendar_windows as calendar


class CalendarAgendaFormatTests(unittest.TestCase):
    def test_mobile_agenda_groups_days_and_keeps_events_distinct(self):
        events = [
            {"summary": "Workout: PUSH/PULL", "start": {"date": "2026-09-29"},
             "end": {"date": "2026-09-30"}},
            {"summary": "Practice", "start": {"dateTime": "2026-09-29T18:45:00-04:00"},
             "end": {"dateTime": "2026-09-29T19:30:00-04:00"}},
            {"summary": "School trip", "start": {"date": "2026-10-02"},
             "end": {"date": "2026-10-03"}},
        ]
        text = calendar.render_events("This Week", events)
        self.assertEqual(text, (
            "**This Week** · 3 events\n\n"
            "**Tuesday, Sep 29**\n\n"
            "- All day — Workout: PUSH/PULL\n"
            "- 6:45 PM–7:30 PM — Practice\n\n"
            "**Friday, Oct 02**\n\n"
            "- All day — School trip"
        ))

    def test_local_time_and_provider_title_stay_safe(self):
        text = calendar.render_events("Today", [
            {"summary": "**Important**\n- hidden", "start": {
                "dateTime": "2026-10-01T14:00:00Z"}, "end": {
                "dateTime": "2026-10-01T15:00:00Z"}}
        ])
        self.assertIn("**Thursday, Oct 01**", text)
        self.assertIn(r"- 10:00 AM–11:00 AM — \*\*Important\*\* - hidden", text)
        self.assertNotIn("\n- hidden", text)

    def test_empty_and_malformed_response(self):
        self.assertEqual(calendar.render_events("Today", []),
                         "**Today** · 0 events\n\nNo events scheduled.")
        with self.assertRaises(calendar.CalendarWindowError):
            calendar.render_events("Today", [{"summary": "No start"}])


if __name__ == "__main__":
    unittest.main()
