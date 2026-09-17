"""Guard: the Sunday Routine is DEFERRED — no claim that it already exists.

Blocker B2 records the Sunday Routine as a deferred follow-up that will
compose three capabilities (Facebook OCR, the Glofox Level 6 reader, and an
approval-gated Google Messages sender) only once ALL THREE are implemented.

These checks pin the *absence* of the composition so no test or module can
quietly assert a working Sunday Routine before its prerequisites ship:

  * the deferral record exists and says so explicitly;
  * the Glofox Level 6 reader (the one implemented prerequisite) is present;
  * the messaging sender is NOT wired (the default connector refuses), and the
    Google connector foundation is read-only — so the send prerequisite is
    unmet;
  * no Facebook/OCR reader module exists — so the OCR prerequisite is unmet.

Run: python3 -m pytest test_sunday_routine_deferred.py
"""
import importlib.util
import os
import sys

import pytest

_CLOUD = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.join(_CLOUD, "web", "backend")
for _p in (_CLOUD, _BACKEND):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("WEB_SESSION_SECRET", "sunday-deferred-test-secret")

_DOC = os.path.join(_CLOUD, "SUNDAY_ROUTINE_DEFERRED.md")


def test_deferral_record_exists_and_says_deferred():
    assert os.path.isfile(_DOC), "the Sunday-Routine deferral record is missing"
    with open(_DOC, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert "DEFERRED" in text
    assert "does NOT exist" in text
    # All three prerequisites are named, and the composed routine is gated on
    # all three being implemented.
    for needle in ("Facebook OCR", "Glofox", "Google Messages"):
        assert needle in text, needle


def test_glofox_reader_is_present():
    """The ONE implemented prerequisite: the pinned Level 6 schedule read."""
    import dev_bot
    import serve
    assert serve.GLOFOX_TASK_TEXT == "glofox: schedule"
    assert dev_bot.GLOFOX_SCHEDULE_COMMAND == serve.GLOFOX_TASK_TEXT
    assert callable(dev_bot.submit_glofox_task)


def test_messaging_sender_is_not_wired():
    """The approval boundary exists, but NO provider sends — so a Sunday
    Routine could not deliver anything today."""
    import messaging
    connector = messaging.get_connector()
    assert connector.connector_id == "none"
    with pytest.raises(messaging.MessagingUnavailable):
        connector.deliver(destination="conversation:any", message="draft")


def test_no_facebook_ocr_reader_exists():
    assert importlib.util.find_spec("facebook_ocr") is None
    assert importlib.util.find_spec("facebook") is None
