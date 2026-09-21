"""Deterministic natural-language aliases for the unified Calendar Bot."""

import serve


def test_calendar_read_aliases_are_canonical():
    assert serve.natural_calendar_command("What's on my calendar today?") == "calendar: today"
    assert serve.natural_calendar_command("what do I have tomorrow") == "calendar: tomorrow"
    assert serve.natural_calendar_command("Show the owner their calendar for this week") == "calendar: week"
    assert serve.natural_calendar_command("calendar: week") is None


def test_level6_alias_is_canonical():
    assert serve.natural_level6_calendar_command(
        "Show me this week's Level 6 workouts") == "level6: calendar"
    assert serve.natural_level6_calendar_command(
        "what is the level6 schedule?") == "level6: calendar"


def test_create_language_never_becomes_a_read():
    for text in (
        "Add dentist tomorrow",
        "Schedule a workout on Friday",
        "Create a calendar event for this week",
    ):
        assert serve.natural_calendar_command(text) is None
        assert serve.natural_level6_calendar_command(text) is None
