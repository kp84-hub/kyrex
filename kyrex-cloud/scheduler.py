#!/usr/bin/env python3
"""scheduler.py — Daily calendar report scheduler for Kyrex Cloud.

Schedules a recurring daily calendar report via the "cal" executor.
Default report time is 07:00 UTC, configurable via KYREX_MORNING_REPORT_HOUR.
Time zone configurable via KYREX_MORNING_REPORT_TIMEZONE (default UTC).

Uses serve.run_task() for execution — no transport dependencies.
The send/edit callables are injected by the transport (e.g. telegram_bot.py).
"""

import os
import threading
from datetime import datetime, timedelta, timezone
import zoneinfo

DEFAULT_REPORT_HOUR = 7


class DailyReport:
    """Schedules a daily calendar report via the cal executor.

    Usage::

        report = DailyReport(
            chat_id=CHAT_ID,
            repo_url="https://github.com/user/repo.git",
            send=send_message,
            edit=edit_message,
        )
        report.start()
        ...
        report.stop()

    The report fires at ``KYREX_MORNING_REPORT_HOUR`` (default 07:00) in the
    timezone specified by ``KYREX_MORNING_REPORT_TIMEZONE`` (default UTC).
    After execution it automatically reschedules for the next day.
    """

    def __init__(self, chat_id, repo_url, task_text="list today",
                 send=None, edit=None, session_key=None):
        self.chat_id = chat_id
        self.repo_url = repo_url
        self.task_text = task_text
        self.send = send
        self.edit = edit
        self.session_key = session_key
        self._timer = None
        self._stopped = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @staticmethod
    def report_hour():
        """Return the configured report hour (0-23).

        Reads ``KYREX_MORNING_REPORT_HOUR`` from the environment.
        Falls back to ``DEFAULT_REPORT_HOUR`` (07:00) if unset or invalid.
        """
        raw = os.environ.get("KYREX_MORNING_REPORT_HOUR")
        if raw is not None:
            try:
                val = int(raw)
                if 0 <= val <= 23:
                    return val
            except (ValueError, TypeError):
                pass
        return DEFAULT_REPORT_HOUR

    # ------------------------------------------------------------------
    # Timing
    # ------------------------------------------------------------------

    @staticmethod
    def _seconds_until(hour):
        """Compute seconds from now until the next occurrence of ``hour``:00
        in the configured timezone.

        Reads ``KYREX_MORNING_REPORT_TIMEZONE`` from the environment (default
        ``"UTC"``) and constructs the current time with ``zoneinfo.ZoneInfo``.
        If ``hour`` has already passed today, the target is tomorrow.
        Always returns a positive value.
        """
        tz_name = os.environ.get("KYREX_MORNING_REPORT_TIMEZONE", "UTC")
        tz = zoneinfo.ZoneInfo(tz_name)
        now = datetime.now(tz)
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _execute(self):
        """Timer callback: run the calendar report and reschedule."""
        from serve import run_task, session_lock

        skey = str(self.session_key if self.session_key is not None
                   else self.chat_id)
        lock = session_lock(skey)
        lock.acquire()
        run_task(
            self.chat_id,
            self.repo_url,
            self.task_text,
            executor_prefix="cal",
            send=self.send,
            edit=self.edit,
            session_key=self.session_key,
        )
        # run_task releases the lock in its ``finally`` block.

        with self._lock:
            if not self._stopped:
                self._schedule()

    def _schedule(self):
        """Set the timer for the next report time."""
        delay = self._seconds_until(self.report_hour())
        self._timer = threading.Timer(delay, self._execute)
        self._timer.daemon = True
        self._timer.start()

    def start(self):
        """Begin the daily report schedule.

        Safe to call multiple times — subsequent calls are no-ops while
        already running.
        """
        with self._lock:
            if self._timer is not None:
                return  # already started
            self._stopped = False
            self._schedule()

    def stop(self):
        """Cancel the next scheduled report, if any.

        Does **not** interrupt an already-running report.
        """
        with self._lock:
            self._stopped = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None