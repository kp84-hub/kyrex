"""Deterministic natural-language aliases for the unified Calendar Bot."""

import serve


def test_calendar_read_aliases_are_canonical():
    assert serve.natural_calendar_command("What's on my calendar today?") == "calendar: today"
    assert serve.natural_calendar_command("what do I have tomorrow") == "calendar: tomorrow"
    assert serve.natural_calendar_command("Show the owner their calendar for this week") == "calendar: week"
    assert serve.natural_calendar_command("calendar: week") is None


def test_unqualified_calendar_lookup_uses_upcoming_week_only():
    assert serve.natural_calendar_command("Whats on my calendar") == "calendar: week"
    assert serve.natural_calendar_command("What’s on my calendar?") == "calendar: week"
    assert serve.natural_calendar_command(
        "Read my calendar and return what events are on it, including date, time, and title for each."
    ) == "calendar: week"
    assert serve.natural_calendar_command("What is on my calendar next month?") is None
    assert serve.natural_calendar_command("Read my calendar last week") is None


def test_weekday_and_short_calendar_reads():
    assert serve.natural_calendar_command("what on my calendar") == "calendar: week"
    assert serve.natural_calendar_command("what is on the calender for Friday") == "calendar: friday"
    assert serve.resolve_executor("calendar: friday") == ("calendar", "calendar: friday", None)


def test_bare_weekday_calendar_reads_are_bounded():
    for day in serve.CALENDAR_WEEKDAYS:
        for text in (f"What’s on my calendar {day}", f"Whats on my calender {day}?",
                     f"What is on the calendar {day}?", f"Show me my calendar {day}"):
            assert serve.natural_calendar_command(text) == f"calendar: {day}", text
    for text in ("Add an event to my calendar Friday", "Delete my calendar Friday",
                 "What's on my calendar Friday next month?",
                 "What's on my calendar Friday and Saturday?"):
        assert serve.natural_calendar_command(text) is None, text


def test_weekday_window_is_one_local_day():
    from datetime import datetime
    from calendar_windows import window_bounds
    label, start, end = window_bounds("friday", now=datetime(2026, 9, 29, 12))
    assert label == "Friday, Oct 02"
    assert start.startswith("2026-10-02T00:00:00")
    assert end.startswith("2026-10-03T00:00:00")


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


def test_named_date_calendar_reads(monkeypatch):
    import calendar_windows as cw
    from datetime import datetime
    monkeypatch.setattr(cw, "local_now", lambda now=None: datetime(2026, 9, 30, 12, tzinfo=cw.CALENDAR_TZ))
    for text in ("What’s on my calendar October 16?", "What's on my calendar for Oct 16th?",
                 "Show my calendar on October 16, 2026", "Check my calendar 2026-10-16"):
        assert serve.natural_calendar_command(text) == "calendar: 2026-10-16", text
    assert serve.natural_calendar_command("What's on my calendar October 16, 2027?") == "calendar: 2027-10-16"
    for text in ("What's on my calendar February 30?", "What's on my calendar October 16 and 17?",
                 "What's on my calendar October 16 and delete Fair", "Add Fair to my calendar October 16"):
        assert serve.natural_calendar_command(text) is None, text
    assert serve.resolve_executor("calendar: 2026-10-16") == ("calendar", "calendar: 2026-10-16", None)
    assert serve.calendar_window_for_task("calendar: 2026-02-30") is None
    assert serve.calendar_window_for_task("calendar: 2026-10-16; delete") is None


def test_event_search_phrases_are_normalized_and_bounded():
    request = ("Look up calendar events for Stella heartworm meds and tell me "
               "any matching dates/times.")
    canonical = serve.natural_calendar_search_command(request)
    assert canonical == "calendar: search Stella heartworm meds"
    assert serve.calendar_search_query_for_task(canonical) == "Stella heartworm meds"
    assert serve.calendar_task_supported(canonical)
    assert serve.resolve_executor(canonical) == (
        "calendar", "calendar: search stella heartworm meds", None)
    assert serve.natural_calendar_search_command(
        "When is Stella's heartworm pill?") == "calendar: search Stella's heartworm pill"
    assert serve.natural_calendar_search_command(
        "Can you find anything on the calendar for Stella heart warm meds") == (
            "calendar: search Stella heart warm meds")
    assert serve.natural_calendar_search_command(
        "Could you please find any calendar events on my calendar for Stella's meds?") == (
            "calendar: search Stella's meds")
    assert serve.natural_calendar_search_command(
        "Create a heartworm event tomorrow") is None
    assert serve.natural_calendar_search_command(
        "Delete Stella's heartworm reminder") is None
    assert serve.calendar_search_query_for_task("calendar: search x") is None
    assert serve.calendar_search_query_for_task("calendar: search " + "x" * 121) is None


def test_calendar_search_window_is_bounded_in_local_timezone():
    from datetime import datetime
    from calendar_windows import search_window_bounds
    label, start, end = search_window_bounds(now=datetime(2026, 9, 30, 12))
    assert label.startswith("Calendar search")
    assert start == "2025-09-30T00:00:00-04:00"
    assert end == "2028-09-30T00:00:00-04:00"


def test_explicit_day_window_preserves_dst_and_calendar_year():
    from calendar_windows import window_bounds
    label, start, end = window_bounds("2026-10-16")
    assert label == "Friday, Oct 16, 2026"
    assert start == "2026-10-16T00:00:00-04:00"
    assert end == "2026-10-17T00:00:00-04:00"
    _, start, end = window_bounds("2026-11-01")
    assert start == "2026-11-01T00:00:00-04:00"
    assert end == "2026-11-02T00:00:00-05:00"
