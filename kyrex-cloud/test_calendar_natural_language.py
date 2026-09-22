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


def test_destructive_level6_event_reference_never_routes():
    """A delete/edit referencing a Level 6 EVENT TITLE is not a schedule read.

    The canonical case: removing a specific Level 6 Workout event. It must
    never be mapped onto the Level 6 weekly/schedule READ, on either the
    Level 6 or the plain-calendar detector.
    """
    destructive = (
        "Remove this from calendar Level 6 Workout: Lower Body Pyramid Sets",
        "Delete this from my calendar Level 6 Workout: Lower Body Pyramid Sets",
        "remove the calendar event id abc123XYZ.789",
        "Cancel the Level 6 workout on Friday",
        "Move the Level 6 Workout: Lower Body Pyramid Sets to Monday",
        "Update the Level 6 schedule for next week",
    )
    for text in destructive:
        assert serve.natural_level6_calendar_command(text) is None, text
        assert serve.natural_calendar_command(text) is None, text
        # and it is not one of the byte-exact commands either
        assert text.strip() != serve.LEVEL6_CALENDAR_TASK_TEXT
        assert text.strip() != serve.LEVEL6_TASK_TEXT

    # The explicit schedule READ requests still route.
    assert serve.natural_level6_calendar_command(
        "Show me this week's Level 6 workouts") == "level6: calendar"
    assert serve.natural_level6_calendar_command(
        "what is the level6 schedule?") == "level6: calendar"


def test_level6_prefix_handler_is_byte_exact():
    """Only the exact `level6: weekly`/`level6: calendar` text reaches the
    structured handler; a longer delete sentence with the prefix is rejected."""
    assert serve.resolve_executor("level6: weekly") == ("level6", "weekly", None)
    assert serve.resolve_executor("level6: calendar") == ("level6", "calendar", None)
    prefix, _text, err = serve.resolve_executor(
        "level6: Remove this from calendar Level 6 Workout: Lower Body Pyramid Sets")
    assert prefix is None and err == "level6"
