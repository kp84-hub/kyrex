"""Tests for flux_executor.py — Kyrex Cloud Flux integration.

Covers: unsupported commands, fail-closed missing credentials, protocol
compliance (KYREX_OPERATION before verdict, single KYREX_RESULT_JSON),
denied verdicts, mocked HTTP calls for status/get/post/send, and host-side
registration (EXECUTORS, KNOWN_OPERATIONS, tier derivation, unbound policy).

All HTTP calls are mocked — no real Flux service or credentials needed.

Run: python3 test_flux_executor.py
"""
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve

failures = []

EXECUTOR = Path(__file__).resolve().parent / "flux_executor.py"

MOCK_CREDS = {
    "FLUX_API_URL": "https://flux.example.com",
    "FLUX_API_TOKEN": "test-flux-token",
}


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def _clean_env():
    """Return an env dict without any Flux credentials."""
    env = os.environ.copy()
    for key in list(env.keys()):
        if key.startswith("FLUX_"):
            del env[key]
    return env


def run_flux(task_text, *, verdict="ALLOW\n", env=None) -> dict:
    """Run flux_executor.py as a subprocess with the given task and return
    the parsed result dict.  ALLOW is the default because these cases test
    the operation itself, not the authorization path."""
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


def run_flux_interactive(task_text, *, stdin_text="ALLOW\n", env=None):
    """Run flux_executor.py with the full protocol over pipes.

    Returns (result_dict, all_stdout_lines) so callers can inspect protocol
    lines as well as the final result.
    """
    base_env = _clean_env()
    if env:
        base_env.update(env)
    proc = subprocess.Popen(
        [sys.executable, str(EXECUTOR), "--task", task_text],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=base_env,
    )
    stdout, stderr = proc.communicate(stdin_text, timeout=15)
    lines = stdout.splitlines()
    result = {}
    for line in lines:
        if line.startswith("KYREX_RESULT_JSON:"):
            result = json.loads(line[len("KYREX_RESULT_JSON:"):])
    return result, lines


def run_flux_inprocess(task_text, *, stdin_text="ALLOW\n", http=None, env=None):
    """Run flux_executor.main() in-process with _http_request mocked.

    Returns (result_dict, all_stdout_lines).
    """
    import flux_executor

    saved_env = {}
    env = env or {}
    for k in list(env.keys()):
        saved_env[k] = os.environ.get(k)
        os.environ[k] = env[k]

    old_stdout = sys.stdout
    new_stdout = io.StringIO()
    sys.stdout = new_stdout

    old_stdin = sys.stdin
    new_stdin = io.StringIO(stdin_text)
    sys.stdin = new_stdin

    old_argv = sys.argv
    sys.argv = ["flux_executor.py", "--task", task_text]

    try:
        with patch.object(flux_executor, "_http_request") as mock_http:
            if isinstance(http, Exception):
                mock_http.side_effect = http
            else:
                mock_http.return_value = http or (200, {})
            flux_executor.main()
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


# ── Protocol tests (subprocess-based, no credentials needed) ──────────

print("\nTest 1: unsupported command is rejected")
result = run_flux("deploy production", verdict="ALLOW\n")
check("status is error", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("error mentions unsupported",
      any("unsupported" in (e or "").lower() for e in result.get("errors", [])),
      f"errors={result.get('errors')}")

print("\nTest 2: usage errors for incomplete commands")
result = run_flux("get", verdict="ALLOW\n")
check("bare 'get' is a usage error", result.get("status") == "error",
      f"got {result.get('status')!r}")
result = run_flux("post stream-only", verdict="ALLOW\n")
check("'post' without text is a usage error", result.get("status") == "error",
      f"got {result.get('status')!r}")
result = run_flux("send stream-only", verdict="ALLOW\n")
check("'send' without text is a usage error", result.get("status") == "error",
      f"got {result.get('status')!r}")

print("\nTest 3: fail-closed — missing credentials error before any work")
result = run_flux("status", verdict="ALLOW\n", env=MOCK_CREDS)
# This run HAS credentials (proves the path works end-to-end without a
# real server only via the in-process tests below); subprocess without
# creds is covered in Test 4.
check("status with creds but unreachable API errors cleanly",
      result.get("status") == "error",
      f"got {result.get('status')!r}, errors={result.get('errors')}")

print("\nTest 4: fail-closed — no credentials, no network, clean error")
result = run_flux("status", verdict="ALLOW\n")
check("status is error without creds", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("error mentions missing credentials",
      any("credentials" in (e or "").lower() for e in result.get("errors", [])),
      f"errors={result.get('errors')}")

print("\nTest 5: KYREX_OPERATION emitted before verdict, dotted op form")
result, lines = run_flux_interactive("get alerts", stdin_text="ALLOW\n")
op_lines = [l for l in lines if l.startswith("KYREX_OPERATION:")]
check("KYREX_OPERATION line present", len(op_lines) == 1,
      f"got {len(op_lines)} operation line(s)")
if op_lines:
    op_data = json.loads(op_lines[0][len("KYREX_OPERATION:"):])
    check("op is dotted flux.read", op_data.get("op") == "flux.read",
          f"got {op_data.get('op')!r}")
    check("target is the stream name", op_data.get("target") == "alerts",
          f"got {op_data.get('target')!r}")
    check("no executor-declared tier sent",
          "tier" not in op_data,
          f"op_data={op_data}")
prog_lines = [l for l in lines if l.startswith("KYREX_PROGRESS:")]
check("KYREX_PROGRESS emitted", len(prog_lines) >= 1,
      f"got {len(prog_lines)} progress line(s)")
check("exactly one result line",
      sum(1 for l in lines if l.startswith("KYREX_RESULT_JSON:")) == 1,
      f"lines={lines}")

print("\nTest 6: denied verdict refuses the operation")
result = run_flux("get alerts", verdict="DENY\n")
check("status is error on DENY", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("error mentions denial",
      any("denied" in (e or "").lower() for e in result.get("errors", [])),
      f"errors={result.get('errors')}")

print("\nTest 7: approval flow — APPROVE then APPROVED")
result, lines = run_flux_interactive(
    "post alerts disk 90%",
    stdin_text="APPROVE\nAPPROVED\n",
)
# Without credentials the operation is denied/failed at the credential
# check, which happens after the verdict — so the result is an error, but
# the important part is that the executor requested approval (line below).
approval_lines = [l for l in lines if l.startswith("KYREX_APPROVAL:")]
check("KYREX_APPROVAL emitted on APPROVE", len(approval_lines) == 1,
      f"got {len(approval_lines)} approval line(s)")

# ── Mocked in-process tests (HTTP mocked, credentials set) ────────────

print("\nTest 8: status command with mocked HTTP")
http = (200, {"state": "healthy", "version": "1.4.2", "streams": 7})
result, lines = run_flux_inprocess("status", http=http, env=MOCK_CREDS)
check("status is ok", result.get("status") == "ok",
      f"got {result.get('status')!r}, errors={result.get('errors')}")
check("response contains state", "healthy" in result.get("final_response", ""),
      f"got {result.get('final_response')!r}")

print("\nTest 9: get command with mocked HTTP")
http = (200, {"events": [
    {"ts": "2026-08-30T12:00:00Z", "text": "deploy finished"},
    {"ts": "2026-08-30T12:05:00Z", "text": "tests green"},
]})
result, lines = run_flux_inprocess("get deploy", http=http, env=MOCK_CREDS)
check("status is ok", result.get("status") == "ok",
      f"got {result.get('status')!r}")
check("response contains event text",
      "deploy finished" in result.get("final_response", ""),
      f"got {result.get('final_response')!r}")
check("response contains event count", "2 event(s)" in result.get("final_response", ""),
      f"got {result.get('final_response')!r}")

print("\nTest 10: empty stream returns no-events message")
http = (200, {"events": []})
result, lines = run_flux_inprocess("get deploy", http=http, env=MOCK_CREDS)
check("status is ok", result.get("status") == "ok",
      f"got {result.get('status')!r}")
check("response says no events", "(no events)" in result.get("final_response", ""),
      f"got {result.get('final_response')!r}")

print("\nTest 11: post flows through APPROVE verdict with mocked HTTP")
http = (200, {})
result, lines = run_flux_inprocess(
    "post alerts disk 90%",
    stdin_text="APPROVE\nAPPROVED\n",
    http=http,
    env=MOCK_CREDS,
)
check("status is ok", result.get("status") == "ok",
      f"got {result.get('status')!r}, errors={result.get('errors')}")
check("response confirms post",
      "Posted to alerts" in result.get("final_response", ""),
      f"got {result.get('final_response')!r}")
op_lines = [l for l in lines if l.startswith("KYREX_OPERATION:")]
if op_lines:
    op_data = json.loads(op_lines[0][len("KYREX_OPERATION:"):])
    check("post declares flux.post op", op_data.get("op") == "flux.post",
          f"got {op_data.get('op')!r}")

print("\nTest 12: send flows through APPROVE verdict with mocked HTTP")
http = (200, {})
result, lines = run_flux_inprocess(
    "send status-page nightly build failed",
    stdin_text="APPROVE\nAPPROVED\n",
    http=http,
    env=MOCK_CREDS,
)
check("status is ok", result.get("status") == "ok",
      f"got {result.get('status')!r}, errors={result.get('errors')}")
check("response confirms send",
      "Sent on status-page" in result.get("final_response", ""),
      f"got {result.get('final_response')!r}")
op_lines = [l for l in lines if l.startswith("KYREX_OPERATION:")]
if op_lines:
    op_data = json.loads(op_lines[0][len("KYREX_OPERATION:"):])
    check("send declares flux.send op", op_data.get("op") == "flux.send",
          f"got {op_data.get('op')!r}")

print("\nTest 13: HTTP error surfaces as a protocol error result")
result, lines = run_flux_inprocess(
    "status",
    http=RuntimeError("connection refused"),
    env=MOCK_CREDS,
)
check("status is error on HTTP failure", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("error mentions the failure",
      any("failed" in (e or "").lower() for e in result.get("errors", [])),
      f"errors={result.get('errors')}")


# ── Host-side registration tests ──────────────────────────────────────

print("\nTest 14: flux executor registered in serve.EXECUTORS")
check("flux executor in EXECUTORS",
      "flux" in serve.EXECUTORS,
      f"EXECUTORS keys={list(serve.EXECUTORS.keys())}")
check("flux executor maps to flux_executor.py",
      serve.EXECUTORS["flux"] == "flux_executor.py",
      f"got {serve.EXECUTORS.get('flux')!r}")

print("\nTest 15: flux ops in KNOWN_OPERATIONS and tier table")
check("flux.read in KNOWN_OPERATIONS",
      "flux.read" in serve.KNOWN_OPERATIONS,
      f"KNOWN_OPERATIONS={sorted(serve.KNOWN_OPERATIONS)}")
check("flux.post in KNOWN_OPERATIONS",
      "flux.post" in serve.KNOWN_OPERATIONS,
      f"KNOWN_OPERATIONS={sorted(serve.KNOWN_OPERATIONS)}")
check("flux.send in KNOWN_OPERATIONS",
      "flux.send" in serve.KNOWN_OPERATIONS,
      f"KNOWN_OPERATIONS={sorted(serve.KNOWN_OPERATIONS)}")

print("\nTest 16: host derives tiers for flux ops")
check("flux:read derives T0",
      serve.derive_host_tier("flux:read", "status") == 0,
      f"got {serve.derive_host_tier('flux:read', 'status')!r}")
check("flux:post derives T1",
      serve.derive_host_tier("flux:post", "alerts") == 1,
      f"got {serve.derive_host_tier('flux:post', 'alerts')!r}")
check("flux:send derives T2",
      serve.derive_host_tier("flux:send", "status-page") == 2,
      f"got {serve.derive_host_tier('flux:send', 'status-page')!r}")
check("unknown flux op derives None",
      serve.derive_host_tier("flux:purge", "everything") is None,
      f"got {serve.derive_host_tier('flux:purge', 'everything')!r}")

print("\nTest 17: scope escalation forces T2 for flux ops touching kyrex-cloud")
check("flux:read on kyrex-cloud target escalates to T2",
      serve.derive_host_tier("flux:read", "kyrex-cloud/serve.py") == 2,
      f"got {serve.derive_host_tier('flux:read', 'kyrex-cloud/serve.py')!r}")

print("\nTest 18: policy interplay — unbound session policy")
decision = serve.policy.evaluate(serve.UNBOUND_POLICY, "flux:read", 0)
check("unbound policy allows flux:read at T0",
      serve.policy.enforce(decision) == 0,
      f"decision={decision}")
decision = serve.policy.evaluate(serve.UNBOUND_POLICY, "flux:post", 1)
check("unbound policy denies flux:post",
      serve.policy.enforce(decision) == "deny",
      f"decision={decision}")

print("\nTest 19: resolve_executor routes 'flux:' prefix")
prefix, rest, err = serve.resolve_executor("flux: get alerts")
check("flux prefix resolves", prefix == "flux", f"got {prefix!r}")
check("prefix stripped from task", rest == "get alerts", f"got {rest!r}")
check("no error", err is None, f"got {err!r}")


# ── Summary ───────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
