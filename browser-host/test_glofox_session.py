"""Readiness is distinct from a verified signed-out member portal."""
from types import SimpleNamespace

import pytest

import workout_booking_driver as adapter
import workout_booking_schema as schema

PORTAL = 'https://app.glofox.com/portal/#/branch/' + schema.BRANCH + '/classes-list-view'


@pytest.fixture
def page(monkeypatch):
    clock = [0.0]
    flags = lambda now: {'signed_in': False, 'signed_out': False}
    frame = SimpleNamespace(url=PORTAL)
    frame.locator = lambda selector: SimpleNamespace(
        evaluate=lambda script, timeout: fake.flags(clock[0]))
    fake = SimpleNamespace(frames=[frame], flags=flags, clock=clock,
                           wait_for_timeout=lambda ms: clock.__setitem__(0, clock[0] + ms / 1000))
    monkeypatch.setattr(adapter.time, 'monotonic', lambda: clock[0])
    return fake


def test_initial_login_link_can_be_replaced_by_a_delayed_saved_session(page):
    page.flags = lambda now: {'signed_in': now >= 2, 'signed_out': now < 2}
    assert adapter.member_session(page, lambda url: True) == 'signed_in'
    assert page.clock[0] >= 2  # The old 700ms check falsely reported logout.


def test_explicit_logout_is_only_reported_after_the_bounded_wait(page):
    page.flags = lambda now: {'signed_in': False, 'signed_out': True}
    assert adapter.member_session(page, lambda url: True) == 'signed_out'
    assert 12 <= page.clock[0] <= 12.25


def test_loading_and_ambiguous_ui_are_not_proof_of_logout(page):
    assert adapter.member_session(page, lambda url: True, timeout_ms=500) == 'session_unverified'
    page.flags = lambda now: {'signed_in': True, 'signed_out': True}
    assert adapter.member_session(page, lambda url: True, timeout_ms=500) == 'session_unverified'


def test_detached_or_unreadable_frame_returns_no_private_error(page):
    def broken(now):
        raise RuntimeError('private account detail')
    page.flags = broken
    assert adapter.member_session(page, lambda url: True, timeout_ms=500) == 'session_unverified'


def test_missing_ambiguous_wrong_branch_and_off_list_frames_are_not_selected(page):
    assert adapter.member_session(page, lambda url: False, timeout_ms=500) == 'unsupported_portal'
    frame = page.frames[0]
    page.frames = [frame, frame]
    assert adapter.member_session(page, lambda url: True, timeout_ms=500) == 'unsupported_portal'
    frame.url = PORTAL.replace(schema.BRANCH, 'a' * 24)
    page.frames = [frame]
    assert adapter.member_session(page, lambda url: True, timeout_ms=500) == 'unsupported_portal'
    page.frames = []
    assert adapter.member_session(page, lambda url: True, timeout_ms=500) == 'unsupported_portal'
