"""Tests for scheduler.py — Daily calendar report scheduler.

Covers:
  1. _seconds_until computes the correct delay for today/tomorrow
  2. Timer fires and invokes run_task with executor_prefix="cal"
  3. stop() prevents execution
  4. Rescheduling after execution

No real Telegram or Google Calendar API calls are made — serve.run_task
is mocked throughout.

Run: python3 test_scheduler.py
"""
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serve  # required for patching serve.run_task and serve.session_lock

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# ── Helpers ────────────────────────────────────────────────────────────

def _make_report(**kwargs):
    """Build a DailyReport with minimal/default args."""
    from scheduler import DailyReport
    opts = dict(
        chat_id=12345,
        repo_url="https://github.com/test/repo.git",
        task_text="list today",
        send=MagicMock(),
        edit=MagicMock(),
        session_key=None,
    )
    opts.update(kwargs)
    return DailyReport(**opts)


def _assert_timer_active(report):
    """Assert the report has a live timer."""
    check("timer exists", report._timer is not None)
    check("timer is alive",
          report._timer is not None and report._timer.is_alive())


def _assert_timer_inactive(report):
    """Assert the report has no live timer."""
    if report._timer is not None:
        check("timer is not alive", not report._timer.is_alive())
    else:
        check("timer is None", report._timer is None)


# =====================================================================
# Test 1: _seconds_until — pure function, no mocking
# =====================================================================

print("\n" + "=" * 60)
print("Test 1: _seconds_until computes correct delay")
print("=" * 60)

from scheduler import DailyReport

# 1a. Hour already passed today → target is tomorrow
print("\n--- 1a: hour already passed today → tomorrow ---")
now = datetime.now(timezone.utc)
past_hour = (now.hour - 1) % 24
delay = DailyReport._seconds_until(past_hour)
expected_today = now.replace(hour=past_hour, minute=0, second=0, microsecond=0)
if expected_today <= now:
    expected_today += timedelta(days=1)
expected_delay = (expected_today - now).total_seconds()
check("delay is positive", delay > 0, f"got {delay}")
check("delay matches expected range",
      abs(delay - expected_delay) < 1.0,  # sub-second tolerance
      f"delay={delay}, expected={expected_delay}")

# 1b. Hour is in the future today → target is today
print("\n--- 1b: hour in the future today → today ---")
future_hour = (now.hour + 2) % 24
delay = DailyReport._seconds_until(future_hour)
expected_future = now.replace(hour=future_hour, minute=0, second=0, microsecond=0)
if expected_future <= now:
    expected_future += timedelta(days=1)
expected_delay = (expected_future - now).total_seconds()
check("delay is positive", delay > 0, f"got {delay}")
check("delay matches expected range",
      abs(delay - expected_delay) < 1.0,
      f"delay={delay}, expected={expected_delay}")

# 1c. Boundary: exactly now → tomorrow (since target <= now fails)
print("\n--- 1c: boundary — hour equals current hour → tomorrow ---")
delay = DailyReport._seconds_until(now.hour)
expected_boundary = now.replace(hour=now.hour, minute=0, second=0, microsecond=0)
if expected_boundary <= now:
    expected_boundary += timedelta(days=1)
expected_delay = (expected_boundary - now).total_seconds()
check("delay is positive", delay > 0, f"got {delay}")
check("delay is approximately 24h or less",
      delay < 86401, f"delay={delay} (too large)")


# =====================================================================
# Test 2: report_hour reads env var and defaults
# =====================================================================

print("\n" + "=" * 60)
print("Test 2: report_hour configuration")
print("=" * 60)

# 2a: Default when env var is absent
saved = os.environ.pop("KYREX_MORNING_REPORT_HOUR", None)
try:
    hour = DailyReport.report_hour()
    check("default hour is 7", hour == 7, f"got {hour}")
finally:
    if saved is not None:
        os.environ["KYREX_MORNING_REPORT_HOUR"] = saved

# 2b: Custom hour from env
os.environ["KYREX_MORNING_REPORT_HOUR"] = "14"
try:
    hour = DailyReport.report_hour()
    check("custom hour is 14", hour == 14, f"got {hour}")
finally:
    os.environ.pop("KYREX_MORNING_REPORT_HOUR", None)

# 2c: Invalid value falls back to default
os.environ["KYREX_MORNING_REPORT_HOUR"] = "invalid"
try:
    hour = DailyReport.report_hour()
    check("invalid falls back to 7", hour == 7, f"got {hour}")
finally:
    os.environ.pop("KYREX_MORNING_REPORT_HOUR", None)

# 2d: Out-of-range value falls back to default
os.environ["KYREX_MORNING_REPORT_HOUR"] = "99"
try:
    hour = DailyReport.report_hour()
    check("out-of-range falls back to 7", hour == 7, f"got {hour}")
finally:
    os.environ.pop("KYREX_MORNING_REPORT_HOUR", None)


# =====================================================================
# Test 3: Timer fires and invokes run_task with executor_prefix="cal"
# =====================================================================

print("\n" + "=" * 60)
print("Test 3: timer fires and invokes run_task with cal prefix")
print("=" * 60)

from scheduler import DailyReport as DR

original_run_task = None

def _capture_run_task(chat_id, repo_url, task_text, **kwargs):
    """Stand-in for serve.run_task that records how it was called."""
    check("executor_prefix is cal",
          kwargs.get("executor_prefix") == "cal",
          f"got {kwargs.get('executor_prefix')!r}")
    check("task_text is list today",
          task_text == "list today",
          f"got {task_text!r}")
    check("chat_id is 12345",
          chat_id == 12345,
          f"got {chat_id!r}")


# Patch session_lock to return a real lock so run_task's release() works
class FakeLock:
    """A lock that tracks acquire/release but doesn't block."""
    def __init__(self):
        self._locked = False
    def acquire(self, blocking=True, timeout=-1):
        self._locked = True
        return True
    def release(self):
        self._locked = False
    def locked(self):
        return self._locked


_SESSION_LOCKS: dict = {}
_SESSION_LOCKS_GUARD = threading.Lock()

def _fake_session_lock(key):
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(str(key))
        if lock is None:
            lock = FakeLock()
            _SESSION_LOCKS[str(key)] = lock
        return lock


print("\n--- 3a: short timer fires and calls run_task ---")
report = _make_report()
# Set a very short delay by overriding _seconds_until
original_seconds_until = DR._seconds_until
DR._seconds_until = staticmethod(lambda h: 0.05)  # 50ms

with patch("serve.run_task", wraps=_capture_run_task) as mock_run, \
     patch("serve.session_lock", side_effect=_fake_session_lock):
    report.start()
    _assert_timer_active(report)
    time.sleep(0.3)  # wait for timer to fire

# After firing, the timer should have been consumed
check("run_task was called", mock_run.called,
      f"call_count={mock_run.call_count}")
# Clean up
report.stop()
DR._seconds_until = original_seconds_until


# =====================================================================
# Test 4: stop() prevents execution
# =====================================================================

print("\n" + "=" * 60)
print("Test 4: stop() prevents execution")
print("=" * 60)

print("\n--- 4a: stop cancels pending timer ---")
report = _make_report()
DR._seconds_until = staticmethod(lambda h: 10.0)  # 10s — long enough

with patch("serve.run_task") as mock_run:
    report.start()
    _assert_timer_active(report)
    report.stop()
    _assert_timer_inactive(report)
    # Wait slightly — the timer should never fire
    time.sleep(0.2)
    check("run_task was NOT called", not mock_run.called,
          f"call_count={mock_run.call_count}")

DR._seconds_until = original_seconds_until

print("\n--- 4b: stop before start is a no-op ---")
report = _make_report()
report.stop()  # nothing to stop
check("no error on stop before start", True)


# =====================================================================
# Test 5: Rescheduling after execution
# =====================================================================

print("\n" + "=" * 60)
print("Test 5: rescheduling after execution")
print("=" * 60)

print("\n--- 5a: timer is reset after _execute completes ---")
report = _make_report()
call_count = [0]

def _run_task_once(*_args, **_kwargs):
    call_count[0] += 1

DR._seconds_until = staticmethod(lambda h: 0.05)

with patch("serve.run_task", side_effect=_run_task_once) as mock_run, \
     patch("serve.session_lock", side_effect=_fake_session_lock):
    report.start()
    time.sleep(0.3)
    check("run_task was called", mock_run.called,
          f"call_count={mock_run.call_count}")
    # After execution, a new timer should be scheduled
    check("timer is re-scheduled", report._timer is not None,
          "timer is None after execution")
    if report._timer is not None:
        check("new timer is alive", report._timer.is_alive(),
              "timer is not alive after reschedule")

report.stop()
DR._seconds_until = original_seconds_until

print("\n--- 5b: stopped report does not reschedule ---")
report = _make_report()
DR._seconds_until = staticmethod(lambda h: 0.05)

with patch("serve.run_task", side_effect=_run_task_once) as mock_run, \
     patch("serve.session_lock", side_effect=_fake_session_lock):
    report.start()
    report.stop()
    time.sleep(0.3)
    # stop was called before the timer fired, so run_task should not be called
    # (timer was cancelled)
    check("run_task was NOT called after stop",
          not mock_run.called or call_count[0] == 0,
          f"call_count={mock_run.call_count}")

DR._seconds_until = original_seconds_until


# =====================================================================
# Test 6: start() with valid session_key passes it through
# =====================================================================

print("\n" + "=" * 60)
print("Test 6: session_key is passed through to run_task")
print("=" * 60)

report = _make_report(session_key="my-bot")
DR._seconds_until = staticmethod(lambda h: 0.05)

with patch("serve.run_task") as mock_run:
    with patch("serve.session_lock", side_effect=_fake_session_lock):
        report.start()
        time.sleep(0.3)
        if mock_run.called:
            args, kwargs = mock_run.call_args
            check("session_key forwarded",
                  kwargs.get("session_key") == "my-bot",
                  f"got {kwargs.get('session_key')!r}")
        else:
            check("run_task was called", False, "run_task not invoked")

report.stop()
DR._seconds_until = original_seconds_until


# =====================================================================
# Summary
# =====================================================================

print("\n" + "=" * 60)
if not failures:
    print("ALL TESTS PASSED")
else:
    print(f"{len(failures)} FAILURE(S): {failures}")
print("=" * 60)
sys.exit(1 if failures else 0)