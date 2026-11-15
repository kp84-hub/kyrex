"""Natural workout previews stay on the fixed read-only preview workflow."""
import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import pytest
import serve
import delegation

PROMPT = (
    "Prepare the #L6Workout message for this week and preview it for the "
    "Level 6 group chat. Don’t send yet."
)


@pytest.mark.parametrize("prompt", [
    PROMPT,
    "#L6Workout preview",
    "#L6Workout preview please",
    "Can you preview this week's Level 6 workout message?",
    "Draft the Level 6 workout message for the group chat",
    "Show me a preview of the #L6Workout message",
    "Prepare the #L6Workout message. Do not send yet.",
])
def test_preview_wording(prompt):
    assert serve.natural_level6_preview_command(prompt) == "#L6Workout preview"


@pytest.mark.parametrize("prompt", [
    "#L6Workout", "#L6Workout test", "#L6Workout calendar",
    "Show my Level 6 workout schedule", "Preview an email from Kelly",
    "Read my email about #L6Workout preview",
    "Why did the #L6Workout preview return an email?",
    "Preview next week’s Level 6 workout message",
    "Preview #L6Workout for 2026-10-19",
    "Delete the Level 6 workout draft",
    "Do not preview #L6Workout", "Send #L6Workout without a preview",
    "Prepare and send the #L6Workout message",
])
def test_other_requests_keep_their_own_routes(prompt):
    assert serve.natural_level6_preview_command(prompt) is None


def bot(bot_id, policy, owner="alice", status="running"):
    return {"id": bot_id, "owner": owner, "status": status, "policy": policy}


def test_delegation_normalizes_preview_and_requires_calendar_grant(monkeypatch):
    monkeypatch.setenv("KYREX_LEVEL6_SEND_ENABLED", "0")
    calendar = bot("calendar", serve.CALENDAR_PRESET)
    assert delegation._delegated_level6_message(calendar, PROMPT) == serve.LEVEL6_MESSAGE_PREVIEW_REQUEST
    with pytest.raises(delegation.DelegationError):
        delegation._delegated_level6_message(bot("email", {}), PROMPT)


def test_email_fallback_cannot_claim_workout_preview():
    import jev_stream_router as router
    chat = SimpleNamespace(serve=serve)
    hint = {"selected_bot_id": "email-bot", "selected_bot_name": "Email Bot"}
    assert router._gmail_command_for_routed_turn(
        chat, 'gmail: search "Level 6 workouts"', PROMPT, hint=hint) is None
    assert router._gmail_command_for_routed_turn(
        chat, PROMPT, "", hint=hint) is None


def test_chat_preview_bypasses_model_and_old_email_context(monkeypatch):
    import chat_service as chat
    chief = bot("overwatcher", serve.COORDINATOR_PRESET)
    calendar = bot("calendar", serve.CALENDAR_PRESET)
    # An email-looking Calendar name must not change capability-based routing.
    calendar["name"] = "Calendar and email helper"
    conv = {"conversation_id": "test", "bot_id": "overwatcher", "messages": [
        {"role": "assistant", "content": "Kelly Thompson's email says October 9."},
    ], "gmail_results": ["old-email-id"]}
    monkeypatch.setattr(chat, "get_conversation", lambda *a: conv)
    monkeypatch.setattr(chat, "_write", lambda *a: None)
    monkeypatch.setattr(chat, "resolve_bot_for_user", lambda *a: chief)
    monkeypatch.setattr(chat.bots, "load_bots", lambda: {"chief": chief, "calendar": calendar})
    monkeypatch.setattr(chat, "_resolve_provider", lambda *a, **k: pytest.fail("Preview reached model"))
    monkeypatch.setattr(chat.bot_provider, "resolve_bot_provider", lambda *a: pytest.fail("Preview reached model"))
    captured = []

    async def stream(user, saved, target, command, cid, cancel, **kwargs):
        captured.append((user, target["id"], command, kwargs["mode"]))
        yield {"type": "status", "status": "complete", "content": "Preview only — nothing sent."}

    monkeypatch.setattr(chat, "_stream_writable_bot_task", stream)

    async def run():
        return [frame async for frame in chat.stream_chat("alice", "test", PROMPT)]

    frames = asyncio.run(run())
    assert captured == [("alice", "calendar", "#L6Workout preview", "level6_message")]
    assert frames[-1]["content"] == "Preview only — nothing sent."
    assert conv["messages"][-1]["content"] == PROMPT


def test_target_selection_rejects_foreign_stopped_missing_and_ambiguous(monkeypatch):
    import chat_service as chat
    chief = bot("chief", serve.COORDINATOR_PRESET)
    calendar = bot("calendar", serve.CALENDAR_PRESET)
    for registry in (
        {},
        {"foreign": bot("foreign", serve.CALENDAR_PRESET, owner="bob")},
        {"stopped": bot("stopped", serve.CALENDAR_PRESET, status="stopped")},
        {"one": calendar, "two": bot("two", serve.CALENDAR_PRESET)},
    ):
        monkeypatch.setattr(chat.bots, "load_bots", lambda: registry)
        with pytest.raises(chat.ChatUnavailable):
            chat._level6_preview_target("alice", chief)
    assert chat._level6_preview_target("alice", calendar) is calendar
    with pytest.raises(chat.ChatUnavailable):
        chat._level6_preview_target("bob", calendar)
    with pytest.raises(chat.ChatUnavailable):
        chat._level6_preview_target("alice", bot("ordinary", {}))


@pytest.mark.parametrize("scenario", ["matched", "title_conflict", "duplicate", "strict_send"])
def test_preview_executor_reads_six_days_and_never_dispatches_messages(monkeypatch, scenario):
    import bots
    import browser_hosts
    import level6_weekly as weekly
    import glofox_api
    calendar = bot("calendar", serve.CALENDAR_PRESET)
    browser = bot(weekly.BROWSER_BOT_ID, serve.BROWSER_PRESET)
    registry = {calendar["id"]: calendar, browser["id"]: browser}
    monkeypatch.setattr(bots, "get_bot", registry.get)
    monkeypatch.setattr(browser_hosts, "binding_for", lambda *a: "same-host")
    monkeypatch.setattr(serve, "build_context", lambda *a: SimpleNamespace(
        bot_owner="alice", policy=browser["policy"]))
    saturday = ("WHITE OUT FOR LUNG CANCER" if scenario != "matched"
                else "LUNG CANCER WHITE OUT WORKOUT")
    ocr = "\n".join([
        "Level 6 Training",
        "THE WEEKLY SIX",
        "WEEK OF 11.16.26",
        "MONDAY 11.16 Back Squat",
        "TUESDAY 11.17 Front Squat",
        "WEDNESDAY 11.18 Deadlift",
        "THURSDAY 11.19 Bench Press",
        "FRIDAY 11.20 Clean & Jerk",
        f"SATURDAY 11.21 {saturday}",
    ])
    monkeypatch.setattr(serve, "_level6_browser_dispatch", lambda *a, **k: ({
        "status": "no_changes",
        "level6_weekly": {"ocr_text": ocr},
    }, None))
    assert_week = weekly.assert_plausible_week
    monkeypatch.setattr(weekly, "assert_plausible_week",
                        lambda plan, today: assert_week(plan, today=date(2026, 11, 15)))
    events = [{
        "_id": f"event-{day}",
        "name": (glofox_api.SCHEDULED_CLASS_NAME if day < 21
                 else "Lung Cancer White Out Workout!"),
        "time_start": int(datetime(2026, 11, day, 8, 30, tzinfo=glofox_api.BRANCH_TZ).timestamp()),
        "trainers": ["coach"],
        "trainers_obj": [{"_id": "coach", "name": "Coach One"}],
    } for day in range(16, 22)]
    if scenario == "duplicate":
        events.append(dict(events[-1], _id="duplicate", name=glofox_api.SCHEDULED_CLASS_NAME))
    monkeypatch.setattr(glofox_api, "_post_guest_login", lambda: "test")
    monkeypatch.setattr(glofox_api, "get_branch", lambda token: None)
    monkeypatch.setattr(glofox_api, "_query_week_events", lambda *a: events)
    monkeypatch.setattr(glofox_api, "get_trainers", lambda token: {"coach": "Coach One"})
    monkeypatch.setattr(serve, "browser_host_dispatch", lambda *a, **k: pytest.fail("Preview dispatched a send"))
    monkeypatch.setenv("KYREX_LEVEL6_SEND_ENABLED", "1")
    results, replies = [], []
    ctx = SimpleNamespace(bot_owner="alice", bot_id="calendar", policy=calendar["policy"])
    command = (serve.LEVEL6_MESSAGE_REQUEST if scenario == "strict_send"
               else serve.LEVEL6_MESSAGE_PREVIEW_REQUEST)
    serve._run_level6_facebook_message_task(
        ctx, "chat", command,
        lambda cid, text: replies.append(text), on_result=results.append)
    if scenario == "strict_send":
        assert not results
        assert "unexpected 8:30 class name" in replies[0]
        return
    assert results[0]["mode"] == "level6_preview"
    if scenario == "duplicate":
        assert results[0]["status"] == "error"
        assert "duplicate 8:30" in replies[0]
        assert not results[0].get("message_text")
        return
    assert results[0]["count"] == 6
    assert "Monday 2026-11-16 — Back Squat" in replies[0]
    if scenario == "matched":
        assert "Saturday 2026-11-21 — LUNG CANCER WHITE OUT WORKOUT\nTrainer: Coach One" in replies[0]
        assert replies[0].startswith("Preview only — nothing sent.")
        assert results[0]["message_text"].startswith("#L6Workout\n")
    else:
        assert "Saturday 2026-11-21 — Lung Cancer White Out Workout!\nTrainer: Coach One" in replies[0]
        assert "Weekly Six: WHITE OUT FOR LUNG CANCER · Glofox: Lung Cancer White Out Workout!" in replies[0]
        assert "No send draft was prepared." in replies[0]
        assert not results[0]["message_text"]
        import chat_service
        task = {"status": "done", "executor_prefix": "level6", "chat_id": "alice",
                "conversation_id": "chat", "task_text": serve.LEVEL6_MESSAGE_PREVIEW_REQUEST}
        assert chat_service._workout_preview_draft("alice", {"conversation_id": "chat"}, task, results[0]) is None
