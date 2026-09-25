"""Focused regressions for the bounded email -> calendar handoff core
(``email_event.py``) and the sender+organization + topic intent
preservation in the Gmail query derivation (``serve._gmail_query_from``).

Run: python3 -m pytest test_email_to_calendar.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import email_event  # noqa: E402
import serve  # noqa: E402


# ═════════════════════════════════════════════════════════════════════════
# 1. sender + organization + topic intent is preserved (never dropped)
# ═════════════════════════════════════════════════════════════════════════

def test_sender_and_topic_are_both_preserved():
    text = "read the email from Wake Christian about the 4th grade field trip"
    out = serve.natural_gmail_command(text)
    assert out is not None and out.startswith("gmail: read ")
    # BOTH the sender/organization and the topic survive.
    assert 'from:"Wake Christian"' in out
    assert '"the 4th grade field trip"' in out


def test_find_the_email_routes_to_a_bounded_read():
    text = "find the email from Wake Christian about the 4th grade field trip"
    out = serve.natural_gmail_command(text)
    assert out is not None and out.startswith("gmail: read ")
    assert 'from:"Wake Christian"' in out
    assert '"the 4th grade field trip"' in out


def test_topic_before_sender_order_also_preserved():
    text = "read the email about the field trip from Wake Christian"
    out = serve.natural_gmail_command(text)
    assert out is not None and out.startswith("gmail: read ")
    assert 'from:"Wake Christian"' in out
    assert "field trip" in out


def test_single_word_sender_stays_a_bare_from_operator():
    # The pre-existing behaviour is unchanged for a one-word sender.
    assert serve.natural_gmail_command("find emails from Randy") == \
        "gmail: search from:Randy"


def test_topic_only_still_a_bounded_search():
    assert serve.natural_gmail_command(
        "find emails about the Tesla recall") == "gmail: search the Tesla recall"


def test_combined_query_is_bounded():
    long_topic = "the " + ("very " * 200) + "long field trip"
    out = serve.natural_gmail_command(
        "read the email from Wake Christian about " + long_topic)
    assert out is not None
    assert len(out) <= len("gmail: read ") + serve._GMAIL_QUERY_MAX
    assert out.startswith("gmail: read ")


def test_header_only_request_stays_a_search():
    # A header-only request must NOT be mistaken for a sender phrase.
    assert serve.natural_gmail_command(
        "show the subject/from/date of this message") == "gmail: search"


# ═════════════════════════════════════════════════════════════════════════
# 2. event-fact extraction
# ═════════════════════════════════════════════════════════════════════════

_BODY = (
    "Dear Parents,\n"
    "The 4th grade field trip to the museum will be held on November 14, 2025.\n"
    "It runs from 9:00 am to 2:00 pm.\n"
    "Location: Raleigh Museum of Natural Sciences\n"
)


def test_full_event_facts_are_extracted():
    facts = email_event.extract_event_facts(
        subject="4th Grade Field Trip",
        sender="Wake Christian Academy <office@wakechristian.org>",
        date="Mon, 1 Sep 2025 00:00:00 +0000",
        body=_BODY,
    )
    assert facts["title"] == "4th Grade Field Trip"
    assert facts["date"] == "2025-11-14"
    assert facts["start"] == "09:00"
    assert facts["end"] == "14:00"
    assert facts["location"] == "Raleigh Museum of Natural Sciences"
    assert facts["needs"] == []
    title, date, start, end, all_day = email_event.event_intent_args(facts)
    assert (title, date, start, end, all_day) == (
        "4th Grade Field Trip", "2025-11-14", "09:00", "14:00", False)


def test_explicit_event_title_line_wins_over_subject():
    facts = email_event.extract_event_facts(
        subject="Newsletter",
        body="Event: Spring Concert\nWhen: May 3, 2025, 6:30 pm to 8:00 pm\n",
    )
    assert facts["title"] == "Spring Concert"
    assert facts["date"] == "2025-05-03"
    assert facts["start"] == "18:30" and facts["end"] == "20:00"


def test_all_day_event_needs_no_time():
    facts = email_event.extract_event_facts(
        subject="Teacher Workday",
        body="This is an all day event on 2025-03-10.",
    )
    assert facts["all_day"] is True
    assert facts["date"] == "2025-03-10"
    assert facts["needs"] == []


def test_leading_reply_prefix_is_stripped_from_title():
    facts = email_event.extract_event_facts(subject="Re: Fwd: Field Trip")
    assert facts["title"] == "Field Trip"


# ═════════════════════════════════════════════════════════════════════════
# 3. ambiguous / missing dates fail closed
# ═════════════════════════════════════════════════════════════════════════

def test_two_dates_are_ambiguous_and_fail_closed():
    facts = email_event.extract_event_facts(
        subject="Trip",
        body="Choose either May 3, 2025 or May 10, 2025. It starts at 10am.",
    )
    assert facts["date"] is None
    assert facts["date_ambiguous"] is True
    assert "date" in facts["needs"]
    try:
        email_event.event_intent_args(facts)
    except email_event.EmailEventError as exc:
        assert "date" in exc.needs
    else:
        raise AssertionError("ambiguous date must fail closed")


def test_date_without_year_and_no_header_year_is_not_reliable():
    facts = email_event.extract_event_facts(
        subject="Trip", body="The trip is on November 14 at 9:00 am to 2:00 pm.")
    assert facts["date"] is None
    assert "date" in facts["needs"]


def test_year_is_borrowed_from_the_date_header_when_absent_in_body():
    facts = email_event.extract_event_facts(
        subject="Trip", date="Mon, 1 Sep 2025 00:00:00 +0000",
        body="The trip is on November 14 at 9:00 am to 2:00 pm.")
    assert facts["date"] == "2025-11-14"


def test_missing_time_fails_closed():
    facts = email_event.extract_event_facts(
        subject="Trip", body="Field trip to the park on 2025-05-03.")
    assert facts["date"] == "2025-05-03"
    assert facts["start"] is None and facts["end"] is None
    assert "time" in facts["needs"]


def test_ambiguous_times_fail_closed():
    facts = email_event.extract_event_facts(
        subject="Trip",
        body="On 2025-05-03 we meet 9:00 am to 10:00 am, then 1:00 pm to 2:00 pm.")
    assert facts["time_ambiguous"] is True
    assert facts["start"] is None
    assert "time" in facts["needs"]


def test_empty_body_asks_for_missing_facts():
    facts = email_event.extract_event_facts(subject="Field Trip")
    assert "date" in facts["needs"]
    assert "time" in facts["needs"]


# ═════════════════════════════════════════════════════════════════════════
# 4. pronoun handoff detection
# ═════════════════════════════════════════════════════════════════════════

def test_pronoun_handoff_is_detected():
    for text in (
        "add that to my calendar",
        "Add it to my calendar",
        "put that on my calendar",
        "save this to my calendar please",
        "add the email to my calendar",
        "add that field trip to my calendar",
    ):
        assert email_event.is_add_to_calendar_request(text) is True, text


def test_plain_create_grammar_is_not_the_pronoun_handoff():
    for text in (
        "create a calendar event titled X on 2025-01-01 from 09:00 to 10:00",
        "calendar: add that",
        "find emails from Randy",
        "add milk to my shopping list",
    ):
        assert email_event.is_add_to_calendar_request(text) is False, text


# ═════════════════════════════════════════════════════════════════════════
# 5. supported event-detail facts + bounded same-event enrichment
# ═════════════════════════════════════════════════════════════════════════

_DETAILED = (
    "The 4th grade field trip to the science museum is scheduled for "
    "October 17, 2025.\n"
    "Buses leave the school at 8:30 am.\n"
    "Location: Raleigh Museum of Natural Sciences\n"
    "Permission slips are due by October 10.\n"
    "Cost: $12 per student.\n"
    "Parents are welcome to chaperone; please sign up in the front office.\n"
)


def _facts(body, *, subject="4th Grade Field Trip", date=None):
    return email_event.extract_event_facts(
        subject=subject, sender="office@wakechristian.org",
        date=date or "Mon, 6 Oct 2025 00:00:00 +0000", body=body)


def test_supported_detail_facts_are_extracted():
    facts = _facts(_DETAILED)
    assert facts["transportation"] == "Buses leave the school at 8:30 am."
    assert facts["cost"] == "Cost: $12 per student."
    assert facts["deadline"] == "Permission slips are due by October 10."
    assert any("chaperone" in d for d in facts["details"])
    # A detail NAMES its category explicitly -- nothing is inferred.
    assert facts["transportation"].startswith("Buses")


def test_the_deadline_date_does_not_ambiguate_the_event_date():
    facts = _facts(_DETAILED)
    assert facts["date"] == "2025-10-17"        # the EVENT date ...
    assert facts["date_ambiguous"] is False
    assert "October 10" in facts["deadline"]    # ... not the deadline's


def test_render_event_answer_states_missing_time_and_location():
    facts = _facts("The 4th grade field trip is on October 17, 2025.")
    answer = email_event.render_event_answer(facts)
    assert answer.splitlines()[0] == "4th Grade Field Trip \u2014 Oct 17, 2025"
    assert "Time: not found" in answer
    assert "Location: not found" in answer


def test_merge_fills_missing_facts_from_a_same_event_sibling():
    primary = _facts("The 4th grade field trip is on October 17, 2025.\n"
                     "Location: Raleigh Museum\n")
    # The sibling names the SAME event and carries the TIME the primary lacks.
    sibling = _facts("The 4th grade field trip runs from 9:00 am to 2:00 pm "
                     "on October 17, 2025.")
    merged = email_event.merge_event_facts(primary, [sibling])
    assert merged["start"] == "09:00" and merged["end"] == "14:00"
    assert merged["location"] == "Raleigh Museum"      # primary authoritative
    assert "time" not in merged["needs"]
    assert "Time: 9:00 am \u2013 2:00 pm" in email_event.render_event_answer(merged)


def test_merge_conflicting_values_stay_ambiguous():
    primary = _facts("The 4th grade field trip is on October 17, 2025.")
    a = _facts("The trip runs from 9:00 am to 2:00 pm on October 17, 2025.")
    b = _facts("The trip runs from 10:00 am to 3:00 pm on October 17, 2025.")
    merged = email_event.merge_event_facts(primary, [a, b])
    assert merged["start"] is None and merged["end"] is None
    assert "start" in merged["conflicts"] and "end" in merged["conflicts"]
    assert ("Time: conflicting across the emails \u2014 please confirm"
            in email_event.render_event_answer(merged))


def test_a_different_date_is_never_the_same_event():
    primary = _facts("The field trip is on October 17, 2025.")
    # A nearby UNRELATED event (a different date) is a different event ...
    other = _facts("The fall festival runs 6:00 pm to 8:00 pm on "
                   "November 8, 2025.")
    assert email_event.same_event(primary, other) is False
    # ... so it can NEVER contaminate the target's missing facts.
    merged = email_event.merge_event_facts(primary, [other])
    assert merged["start"] is None and merged["end"] is None
    assert (merged["details"] or []) == []


def test_merge_never_overwrites_the_authoritative_primary():
    primary = _facts("The field trip is on October 17, 2025 from 9:00 am to "
                     "10:00 am. Location: Primary Room\n")
    sibling = _facts("The field trip runs 1:00 pm to 2:00 pm on "
                     "October 17, 2025. Location: Other Room\n")
    merged = email_event.merge_event_facts(primary, [sibling])
    assert merged["start"] == "09:00" and merged["end"] == "10:00"
    assert merged["location"] == "Primary Room"
