"""Tests for cal_executor.py — Google Calendar read-only executor.

Covers: list today, list tomorrow, list week, unsupported commands,
authentication error, protocol compliance, and mocked API calls.

All Google API calls are mocked — no real credentials needed.

Run: python3 test_cal_executor.py
"""
import io
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve

failures = []

EXECUTOR = Path(__file__).resolve().parent / "cal_executor.py"


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def _clean_env():
    """Return an env dict without any Google Calendar / OAuth vars."""
    env = os.environ.copy()
    for key in list(env.keys()):
        if key.startswith("GOOGLE_") or key in (
            "GOOGLE_APPLICATION_CREDENTIALS",
        ):
            del env[key]
    return env


def run_cal(task_text, *, verdict="ALLOW\n", env=None) -> dict:
    """Run cal_executor.py with the given task and return the parsed result
    dict.  ALLOW is the default because these cases test the operation
    itself, not the authorization path."""
    base_env = _clean_env()
    if env:
        base_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(EXECUTOR), "--task", task_text],
        input=verdict,
        capture_output=True, text=True, timeout=15,
        env=base_env,
    )
    if proc.stdout.strip() and not proc.stdout.strip().startswith("KYREX_"):
        check("no stray text on stdout", False,
              f"got non-protocol output: {proc.stdout.strip()!r}")
    for line in proc.stdout.splitlines():
        if line.startswith("KYREX_RESULT_JSON:"):
            return json.loads(line[len("KYREX_RESULT_JSON:"):])
    check("result line present", False,
          f"stdout={proc.stdout.strip()!r}, stderr={proc.stderr.strip()!r}")
    return {}


def run_cal_interactive(task_text, *, stdin_text="", env=None) -> tuple[dict, list[str]]:
    """Run cal_executor.py with approval protocol over stdin.

    Returns (result_dict, all_stdout_lines) so the caller can inspect
    protocol lines as well as the final result.
    """
    base_env = _clean_env()
    if env:
        base_env.update(env)
    proc = subprocess.Popen(
        [sys.executable, str(EXECUTOR), "--task", task_text],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=base_env,
    )
    stdout, stderr = proc.communicate(input=stdin_text, timeout=15)
    if stdout.strip() and not stdout.strip().startswith("KYREX_"):
        check("no stray text on stdout", False,
              f"got non-protocol output: {stdout.strip()!r}")
    lines = stdout.splitlines()
    result_json = {}
    for line in lines:
        if line.startswith("KYREX_RESULT_JSON:"):
            result_json = json.loads(line[len("KYREX_RESULT_JSON:"):])
    if not result_json:
        check("result line present", False,
              f"stdout={stdout.strip()!r}, stderr={stderr.strip()!r}")
    return result_json, lines


# ── Mocked in-process test helper ──────────────────────────────────────

def run_cal_inprocess(task_text, *, stdin_text="ALLOW\n", mock_service=None,
                      env=None):
    """Run cal_executor.main() in-process with mocked _build_service.

    Returns (result_dict, all_stdout_lines).
    """
    import cal_executor

    # Save and override env vars
    env = env or {}
    saved_env = {}
    for k in list(env.keys()):
        saved_env[k] = os.environ.get(k)
        os.environ[k] = env[k]

    # Redirect stdout
    old_stdout = sys.stdout
    new_stdout = io.StringIO()
    sys.stdout = new_stdout

    # Redirect stdin
    old_stdin = sys.stdin
    new_stdin = io.StringIO(stdin_text)
    sys.stdin = new_stdin

    # Set sys.argv so argparse in cal_executor.main() works
    old_argv = sys.argv
    sys.argv = ["cal_executor.py", "--task", task_text]

    try:
        with patch.object(cal_executor, "_build_service",
                          return_value=mock_service) as mock_build:
            cal_executor.main()
            mock_build.assert_called_once()
    except SystemExit:
        pass
    finally:
        sys.stdout = old_stdout
        sys.stdin = old_stdin
        sys.argv = old_argv
        for k in list(env.keys()):
            if saved_env[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved_env[k]

    output = new_stdout.getvalue()
    lines = output.splitlines()
    result_json = {}
    for line in lines:
        if line.startswith("KYREX_RESULT_JSON:"):
            result_json = json.loads(line[len("KYREX_RESULT_JSON:"):])
    return result_json, lines


# ── Test data ──────────────────────────────────────────────────────────

MOCK_EVENTS = [
    {
        "summary": "Morning standup",
        "start": {"dateTime": "2025-01-15T09:00:00+00:00"},
        "end": {"dateTime": "2025-01-15T09:15:00+00:00"},
    },
    {
        "summary": "Lunch with team",
        "start": {"dateTime": "2025-01-15T12:00:00+00:00"},
        "end": {"dateTime": "2025-01-15T13:00:00+00:00"},
    },
]

MOCK_EVENTS_WEEK = [
    {
        "summary": "All-hands meeting",
        "start": {"dateTime": "2025-01-17T15:00:00+00:00"},
        "end": {"dateTime": "2025-01-17T16:00:00+00:00"},
    },
]

MOCK_CREDS = {
    "GOOGLE_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
    "GOOGLE_CLIENT_SECRET": "test-client-secret",
    "GOOGLE_REFRESH_TOKEN": "test-refresh-token",
}


# ── Protocol tests (subprocess-based) ──────────────────────────────────

# 1. Unsupported command — no auth needed
print("\nTest 1: unsupported command is rejected")
result = run_cal("create event", verdict="ALLOW\n")
check("status is error", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("error message mentions unsupported",
      any("unsupported" in (e or "").lower() for e in result.get("errors", [])),
      f"errors={result.get('errors')}")


# 2. Unrecognised list subcommand — no auth needed
print("\nTest 2: unrecognised list subcommand is rejected")
result = run_cal("list yesterday", verdict="ALLOW\n")
check("status is error", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("error message mentions supported commands",
      any("supported" in (e or "").lower() or "list today" in (e or "").lower()
          for e in result.get("errors", [])),
      f"errors={result.get('errors')}")


# 3. Protocol: KYREX_OPERATION line is emitted before verdict
print("\nTest 3: operation line is emitted before waiting for verdict")
result, lines = run_cal_interactive("list today", stdin_text="ALLOW\n")
op_lines = [l for l in lines if l.startswith("KYREX_OPERATION:")]
check("KYREX_OPERATION line present", len(op_lines) >= 1,
      f"got {len(op_lines)} operation line(s)")
if op_lines:
    op_data = json.loads(op_lines[0][len("KYREX_OPERATION:"):])
    check("op field is cal.list",
          op_data.get("op") == "cal.list",
          f"got {op_data.get('op')!r}")
    check("target is the command",
          op_data.get("target") == "list today",
          f"got {op_data.get('target')!r}")


# 4. Protocol: denied by host → error result
print("\nTest 4: denied by host returns error")
result, lines = run_cal_interactive("list today", stdin_text="DENY\n")
check("status is error", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("error mentions denied",
      any("denied" in (e or "").lower() for e in result.get("errors", [])),
      f"errors={result.get('errors')}")


# 5. Missing credentials → auth error before API call
print("\nTest 5: missing credentials returns auth error")
result = run_cal("list today", verdict="ALLOW\n")
check("status is error", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("error mentions authentication",
      any("authentication" in (e or "").lower()
          or "missing" in (e or "").lower()
          for e in result.get("errors", [])),
      f"errors={result.get('errors')}")


# 6. Protocol: exactly one KYREX_RESULT_JSON line
print("\nTest 6: protocol emits exactly one result line")
result, lines = run_cal_interactive("list today", stdin_text="DENY\n")
result_lines = [l for l in lines if l.startswith("KYREX_RESULT_JSON:")]
check("exactly one result line", len(result_lines) == 1,
      f"got {len(result_lines)} result line(s)")


# 7. Protocol: KYREX_PROGRESS line emitted
print("\nTest 7: progress line is emitted")
result, lines = run_cal_interactive("list today", stdin_text="DENY\n")
progress_lines = [l for l in lines if l.startswith("KYREX_PROGRESS:")]
check("at least one progress line", len(progress_lines) >= 1,
      f"got {len(progress_lines)} progress line(s)")


# ── Mocked API tests (in-process with patching) ────────────────────────

# 8. list today with mocked API
print("\nTest 8: list today with mocked Google API")
mock_service = MagicMock()
mock_events = mock_service.events.return_value
mock_list = mock_events.list.return_value
mock_list.execute.return_value = {"items": MOCK_EVENTS}

result, lines = run_cal_inprocess(
    "list today",
    env=MOCK_CREDS,
    mock_service=mock_service,
)
check("status is ok", result.get("status") == "ok",
      f"got {result.get('status')!r}")
response = result.get("final_response", "")
check("response contains event count", "2 event(s)" in response,
      f"got {response!r}")
check("response contains Morning standup", "Morning standup" in response,
      f"got {response!r}")
check("response contains Lunch with team", "Lunch with team" in response,
      f"got {response!r}")

# Verify the API was called with correct parameters
call_kwargs = mock_events.list.call_args[1] if mock_events.list.call_args else {}
check("calendarId is primary",
      call_kwargs.get("calendarId") == "primary",
      f"got {call_kwargs.get('calendarId')!r}")
check("singleEvents is True",
      call_kwargs.get("singleEvents") is True,
      f"got {call_kwargs.get('singleEvents')!r}")
check("orderBy is startTime",
      call_kwargs.get("orderBy") == "startTime",
      f"got {call_kwargs.get('orderBy')!r}")


# 9. list tomorrow with mocked API
print("\nTest 9: list tomorrow with mocked Google API")
mock_service = MagicMock()
mock_events = mock_service.events.return_value
mock_list = mock_events.list.return_value
mock_list.execute.return_value = {"items": MOCK_EVENTS}

result, lines = run_cal_inprocess(
    "list tomorrow",
    env=MOCK_CREDS,
    mock_service=mock_service,
)
check("status is ok", result.get("status") == "ok",
      f"got {result.get('status')!r}")
response = result.get("final_response", "")
check("response contains Tomorrow", "Tomorrow" in response,
      f"got {response!r}")


# 10. list week with mocked API
print("\nTest 10: list week with mocked Google API")
mock_service = MagicMock()
mock_events = mock_service.events.return_value
mock_list = mock_events.list.return_value
mock_list.execute.return_value = {"items": MOCK_EVENTS_WEEK}

result, lines = run_cal_inprocess(
    "list week",
    env=MOCK_CREDS,
    mock_service=mock_service,
)
check("status is ok", result.get("status") == "ok",
      f"got {result.get('status')!r}")
response = result.get("final_response", "")
check("response contains This Week", "This Week" in response,
      f"got {response!r}")
check("response contains All-hands meeting", "All-hands meeting" in response,
      f"got {response!r}")


# 11. Empty calendar returns no-events message
print("\nTest 11: empty calendar returns no-events message")
mock_service = MagicMock()
mock_events = mock_service.events.return_value
mock_list = mock_events.list.return_value
mock_list.execute.return_value = {"items": []}

result, lines = run_cal_inprocess(
    "list today",
    env=MOCK_CREDS,
    mock_service=mock_service,
)
check("status is ok", result.get("status") == "ok",
      f"got {result.get('status')!r}")
response = result.get("final_response", "")
check("response says no events", "(no events)" in response,
      f"got {response!r}")


# 12. API error returns error result
print("\nTest 12: API error returns error result")
mock_service = MagicMock()
mock_events = mock_service.events.return_value
mock_list = mock_events.list.return_value
mock_list.execute.side_effect = RuntimeError("API quota exceeded")

result, lines = run_cal_inprocess(
    "list today",
    env=MOCK_CREDS,
    mock_service=mock_service,
)
check("status is error", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("error mentions API error",
      any("API" in (e or "") for e in result.get("errors", [])),
      f"errors={result.get('errors')}")


# ── Registration tests ───────────────────────────────────────────────

print("\nTest 13: cal executor registered in serve.EXECUTORS")
check("cal executor in EXECUTORS",
      "cal" in serve.EXECUTORS,
      f"EXECUTORS keys={list(serve.EXECUTORS.keys())}")
check("cal executor maps to cal_executor.py",
      serve.EXECUTORS["cal"] == "cal_executor.py",
      f"got {serve.EXECUTORS.get('cal')!r}")


print("\nTest 14: cal.list in KNOWN_OPERATIONS")
check("cal.list in KNOWN_OPERATIONS",
      "cal.list" in serve.KNOWN_OPERATIONS,
      f"KNOWN_OPERATIONS={sorted(serve.KNOWN_OPERATIONS)}")


# ── Summary ───────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)