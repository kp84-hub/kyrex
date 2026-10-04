"""Run with python3 -m unittest kyrex-cloud/test_level6_calendar_batch.py."""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cal_writer  # noqa: E402
import connectors  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent / "web" / "backend"))
import dev_bot  # noqa: E402
import level6_calendar_batch as batch  # noqa: E402
import serve  # noqa: E402
import task_store  # noqa: E402


LINES = [
    f"{day} 2026-{date} — {workout} — trainer: Trainer {index}"
    for index, (day, date, workout) in enumerate([
        ("Monday", "09-28", "ABS & GLUTES"),
        ("Tuesday", "09-29", "PUSH/PULL"),
        ("Wednesday", "09-30", "MUSCULAR ENDURANCE TRAINING"),
        ("Thursday", "10-01", "COREDIO"),
        ("Friday", "10-02", "LOWER BODY LOCKDOWN"),
        ("Saturday", "10-03", "METABOLIC MELTDOWN"),
    ], 1)
]


class BatchTests(unittest.TestCase):
    def test_pronoun_resolves_latest_complete_workout_preview_only(self):
        preview = "#L6Workout\n🏋️ Level 6 — Workout Week\n" + "\n".join(LINES)
        messages = [{"role": "assistant", "content": preview}]
        for text in ("Can you add those to my calendar", "Put them on my calendar?",
                     "Please add these workouts to my calendar"):
            self.assertTrue(batch.is_workout_followup(text, messages))
        screenshot_preview = preview.replace(" — trainer: ", "\nTrainer: ")
        self.assertTrue(batch.is_workout_followup("Can you add those to my calendar",
            [{"role": "assistant", "content": "```text\n" + screenshot_preview + "\n```"}]))
        for text in ("Add the email to my calendar", "Add those to Bob's calendar",
                     "Add those to my calendar and send a message"):
            self.assertFalse(batch.is_workout_followup(text, messages))
        self.assertFalse(batch.is_workout_followup("Add those to my calendar", []))
        self.assertFalse(batch.is_workout_followup("Add those to my calendar",
            messages + [{"role": "assistant", "content": "Here is your email."}]))
        self.assertFalse(batch.is_workout_followup("Add those to my calendar",
            [{"role": "user", "content": preview}]))
        self.assertFalse(batch.is_workout_followup("Add those to my calendar",
            [{"role": "assistant", "content": preview.replace("2026-09-29", "2026-10-29")}]))
        self.assertTrue(batch.is_workout_followup("Add those to my calendar",
            messages + [{"role": "assistant", "content":
                "I don't have a selected email to add. Ask me to read one first."}]))

    def test_calendar_bot_natural_route_and_owner_scoped_submission(self):
        bot = {"id": "calendar", "owner": "alice", "status": "running",
               "policy": serve.calendar_preset_policy()}
        text = "Add this week's Level 6 workouts to my calendar"
        self.assertTrue(dev_bot.level6_calendar_batch_route_ready(bot, text))
        self.assertTrue(dev_bot.level6_calendar_batch_route_ready(
            bot, "#L6Workout calendar"))
        self.assertFalse(dev_bot.level6_calendar_batch_route_ready(
            bot, "Add those workouts to my calendar"))
        self.assertFalse(dev_bot.level6_calendar_batch_route_ready(
            {**bot, "status": "stopped"}, text))
        submitted = []
        store = types.SimpleNamespace(
            submit=lambda **kw: submitted.append(kw) or "task-test")
        self.assertEqual(dev_bot.submit_level6_calendar_batch_task(
            "alice", bot, store=store), "task-test")
        self.assertEqual(submitted[0]["task_text"],
                         serve.LEVEL6_CALENDAR_BATCH_REQUEST)
        self.assertEqual(submitted[0]["executor_prefix"], "level6")
        with self.assertRaises(dev_bot.DevBotError):
            dev_bot.submit_level6_calendar_batch_task("other", bot, store=store)
        self.assertEqual(len(submitted), 1)

    def test_bounded_natural_request_and_six_all_day_intents(self):
        self.assertTrue(batch.is_add_request(
            "Add this week's Level 6 workouts to my calendar"))
        self.assertFalse(batch.is_add_request("Add those workouts to my calendar"))
        self.assertFalse(batch.is_add_request(
            "Add this week's Level 6 workouts to someone else's calendar"))
        intents = batch.intents_from_week(LINES)
        self.assertEqual([intent["start"] for intent in intents], [
            "2026-09-28", "2026-09-29", "2026-09-30",
            "2026-10-01", "2026-10-02", "2026-10-03"])
        self.assertTrue(all(intent["all_day"] for intent in intents))
        self.assertEqual(intents[0]["title"],
                         "Level 6 Workout: ABS & GLUTES")
        self.assertEqual(batch.week_bounds(intents),
                         ("2026-09-28T00:00:00-04:00",
                          "2026-10-04T00:00:00-04:00"))
        for bad in (LINES[:5], LINES[:1] + [LINES[0]] + LINES[2:]):
            with self.assertRaises(batch.CalendarBatchError):
                batch.intents_from_week(bad)

    def test_existing_exact_days_skip_and_conflicts_fail(self):
        intents = batch.intents_from_week(LINES)
        first = intents[0]
        event = {"summary": first["title"], "start": {"date": first["start"]},
                 "end": {"date": first["end"]}}
        self.assertEqual(len(batch.missing_intents(intents, [event])), 5)
        for bad in (
            [event, event],
            [{**event, "summary": "Level 6 Workout: ANOTHER WORKOUT"}],
            [{**event, "start": {"dateTime": "2026-09-28T08:30:00-04:00"}}],
        ):
            with self.assertRaises(batch.CalendarBatchError):
                batch.missing_intents(intents, bad)

    def test_complete_calendar_page_required(self):
        owner = "alice"
        record = {"id": "evt", "summary": "other", "start": {"date": "2026-09-28"}}
        store = types.SimpleNamespace(
            route_capability=lambda *a: {},
            access_token=lambda *a: "test-token")
        reader = connectors.CalendarRead(
            store, owner, transport=lambda *a: {
                "items": [record], "nextPageToken": "another-page"})
        with self.assertRaises(connectors.ConnectorUnavailable):
            reader.events(require_complete=True)

    def test_six_writes_only_after_approval_and_retry_skips_exact_matches(self):
        existing = []
        created = []
        approvals = []
        decisions = [False, True]
        messages = []
        bot = {"id": "calendar", "owner": "alice", "status": "running",
               "policy": {"role": "calendar"}}
        browser = {"id": "browser-bot", "owner": "alice",
                   "status": "running", "policy": {"role": "browser"}}

        def write(event):
            self.assertTrue(approvals)
            created.append(event)
            existing.append({"summary": event["summary"],
                             "start": event["start"], "end": event["end"]})
            return {"id": f"fake-{len(created)}"}

        class FakeConnector:
            def calendar(self, owner):
                self.assert_owner(owner)
                return types.SimpleNamespace(events=lambda **kw: list(existing))

            def calendar_writer(self, owner):
                self.assert_owner(owner)
                return types.SimpleNamespace(create_event=write)

            def preferred_calendar(self, owner):
                self.assert_owner(owner)
                return "primary"

            @staticmethod
            def assert_owner(owner):
                if owner != "alice":
                    raise AssertionError("wrong owner")

        fake_store = FakeConnector()
        fake_weekly = types.SimpleNamespace(
            BROWSER_BOT_ID="browser-bot", run_weekly=lambda **kw: LINES)
        context = types.SimpleNamespace(bot_owner="alice", bot_id="calendar",
                                        policy=bot["policy"])
        browser_context = types.SimpleNamespace(
            bot_owner="alice", policy=browser["policy"])
        fake_task_store = types.SimpleNamespace(
            get=lambda task_id: {"status": task_store.STATUS_RUNNING,
                                 "cancel_requested": False})
        fake_bots = types.SimpleNamespace(
            get_bot=lambda bid: {"calendar": bot, "browser-bot": browser}.get(bid))
        fake_hosts = types.SimpleNamespace(binding_for=lambda *a: "host-1")

        with (patch.dict(sys.modules, {"bots": fake_bots,
                                      "browser_hosts": fake_hosts,
                                      "level6_weekly": fake_weekly}),
              patch.object(serve, "is_calendar_bot_policy",
                           side_effect=lambda p: p == bot["policy"]),
              patch.object(serve, "is_browser_bot_policy",
                           side_effect=lambda p: p == browser["policy"]),
              patch.object(serve, "build_context", return_value=browser_context),
              patch.object(task_store, "CloudTaskStore", return_value=fake_task_store),
              patch.object(connectors, "default_store", return_value=fake_store),
              patch.object(serve, "_request_in_process_approval",
                           side_effect=lambda *a, **kw:
                           approvals.append(kw["detail"]) or decisions.pop(0))):
            for attempt in range(3):
                serve._run_level6_calendar_batch_task(
                    context, "chat", serve.LEVEL6_CALENDAR_BATCH_REQUEST,
                    "task-1", lambda chat, text: messages.append(text) or 1)
                if attempt == 0:
                    self.assertEqual(created, [])
                    self.assertIn("No workouts were added", messages[-1])
        self.assertEqual(len(approvals), 2)
        self.assertEqual(len(approvals[0].splitlines()), 6)
        self.assertEqual(len(created), 6)
        self.assertIn("already on your calendar", messages[-1])
        self.assertEqual(created[0], cal_writer.to_google_event(
            batch.intents_from_week(LINES)[0]))


if __name__ == "__main__":
    unittest.main()
