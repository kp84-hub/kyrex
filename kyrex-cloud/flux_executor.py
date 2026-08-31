#!/usr/bin/env python3
"""flux_executor.py — Kyrex Cloud Flux executor.

Integrates the Flux event-stream service as a Kyrex Cloud executor.
Supported commands:

  status                    — Flux service status/health.            (T0, flux.read)
  get <stream>              — recent events from a stream.           (T0, flux.read)
  post <stream> <text...>   — append an event to a stream.           (T1, flux.post)
  send <stream> <text...>   — deliver an event to external           (T2, flux.send)
                              subscribers.

Tiers are derived host-side (serve.py OPERATION_TIERS); the executor only
declares the operation and waits for the verdict, per the executor contract
in K_BOT_DESIGN.md.

Credentials are read from the environment:

  FLUX_API_URL    — base URL of the Flux API, e.g. https://flux.example.com
  FLUX_API_TOKEN  — bearer token for the Flux API

Missing credentials fail closed before any network I/O.

Protocol: speaks KYREX_PROGRESS:, KYREX_OPERATION: and exactly one
KYREX_RESULT_JSON: line at the end on stdout.  Diagnostics go to stderr.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Flux API helpers
# ---------------------------------------------------------------------------

def _http_request(method: str, url: str, payload=None, token: str = "",
                  timeout: int = DEFAULT_TIMEOUT) -> tuple[int, dict]:
    """Perform one HTTP call against the Flux API.

    Returns (status_code, parsed_body).  Network/HTTP errors raise; callers
    convert them into protocol error results.
    """
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    try:
        parsed = json.loads(body) if body else {}
    except json.JSONDecodeError:
        parsed = {"raw": body}
    return resp.status, parsed


def _flux_env() -> tuple[str, str]:
    """Return (base_url, token) from the environment, failing closed."""
    base = os.environ.get("FLUX_API_URL")
    token = os.environ.get("FLUX_API_TOKEN")
    missing = []
    if not base:
        missing.append("FLUX_API_URL")
    if not token:
        missing.append("FLUX_API_TOKEN")
    if missing:
        raise RuntimeError(
            "Missing required Flux credentials: " + ", ".join(missing)
        )
    return base.rstrip("/"), token


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def handle_status(base: str, token: str) -> list[str]:
    """Fetch and format the Flux service status."""
    _, body = _http_request("GET", f"{base}/status", token=token)
    state = body.get("state", "unknown")
    version = body.get("version", "?")
    streams = body.get("streams", "?")
    return [
        f"🟢 Flux status: {state} (v{version}, {streams} stream(s))",
    ]


def handle_get(base: str, token: str, stream: str) -> list[str]:
    """Fetch recent events from a stream."""
    _, body = _http_request(
        "GET", f"{base}/streams/{urllib.request.quote(stream)}/events",
        token=token,
    )
    items = body.get("events", [])
    lines = [f"📡 Flux stream {stream!r} ({len(items)} event(s))"]
    if items:
        for ev in items:
            ts = ev.get("ts", "?")
            text = ev.get("text", "(no text)")
            lines.append(f"  {ts}  {text}")
    else:
        lines.append("  (no events)")
    return lines


def handle_post(base: str, token: str, stream: str, text: str) -> list[str]:
    """Append an event to a stream (T1: recoverable mutation)."""
    _http_request(
        "POST", f"{base}/streams/{urllib.request.quote(stream)}/events",
        payload={"text": text}, token=token,
    )
    return [f"✅ Posted to {stream}: {text}"]


def handle_send(base: str, token: str, stream: str, text: str) -> list[str]:
    """Deliver an event to external subscribers (T2: externally visible)."""
    _http_request(
        "POST", f"{base}/streams/{urllib.request.quote(stream)}/deliver",
        payload={"text": text}, token=token,
    )
    return [f"✅ Sent on {stream}: {text}"]


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------

def _emit_operation(op: str, target: str, summary: str) -> None:
    """Emit a KYREX_OPERATION: line for host-side policy evaluation."""
    operation = {
        "op": op,
        "target": target,
        "summary": summary,
    }
    print(f"KYREX_OPERATION:{json.dumps(operation)}", flush=True)


def _get_verdict() -> bool:
    """Read the host's decision after KYREX_OPERATION:.

    Returns True to proceed, False to refuse.
    """
    decision = sys.stdin.readline().strip()
    if decision == "ALLOW":
        return True
    if decision == "APPROVE":
        # Host requires human approval; declare the operation and wait for
        # the second verdict.  The declared tier is a hint the host may
        # raise but never lower.
        print(
            "KYREX_APPROVAL:"
            + json.dumps({"tier": 1, "summary": "flux operation"}),
            flush=True,
        )
        second = sys.stdin.readline().strip()
        return second == "APPROVED"
    # DENY, DENIED, or unrecognised → refuse
    return False


def _error_result(errors: list[str]) -> None:
    result = {"status": "error", "final_response": "", "errors": errors}
    print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)


def _ok_result(final_response: str) -> None:
    result = {"status": "ok", "final_response": final_response, "errors": []}
    print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Kyrex Cloud Flux Executor")
    ap.add_argument("--task", required=True,
                    help="task text, e.g. 'status' or 'post alerts disk full'")
    ap.add_argument("--repo-url", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--base", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    task = args.task.strip()
    lowered = task.lower()
    parts = task.split(maxsplit=2)

    # -- status ------------------------------------------------------------
    if lowered == "status":
        print(f'KYREX_PROGRESS:{{"flux": {json.dumps("status")}}}', flush=True)
        _emit_operation("flux.read", "status", "read Flux service status")
        if not _get_verdict():
            _error_result(["flux read denied: status"])
            return
        try:
            base, token = _flux_env()
            lines = handle_status(base, token)
        except Exception as e:
            _error_result([f"Flux status failed: {e}"])
            return
        _ok_result("\n".join(lines))
        return

    # -- get <stream> ------------------------------------------------------
    if lowered.startswith("get ") or lowered == "get":
        if len(parts) < 2 or not parts[1].strip():
            _error_result(["usage: get <stream>"])
            return
        stream = parts[1].strip()
        print(f'KYREX_PROGRESS:{{"flux": {json.dumps(lowered)}}}', flush=True)
        _emit_operation("flux.read", stream, f"read Flux stream {stream!r}")
        if not _get_verdict():
            _error_result([f"flux read denied: {stream}"])
            return
        try:
            base, token = _flux_env()
            lines = handle_get(base, token, stream)
        except Exception as e:
            _error_result([f"Flux get failed: {e}"])
            return
        _ok_result("\n".join(lines))
        return

    # -- post <stream> <text...> -------------------------------------------
    if lowered.startswith("post ") or lowered == "post":
        if len(parts) < 3 or not parts[1].strip() or not parts[2].strip():
            _error_result(["usage: post <stream> <text...>"])
            return
        stream = parts[1].strip()
        text = parts[2].strip()
        print(f'KYREX_PROGRESS:{{"flux": {json.dumps(lowered)}}}', flush=True)
        _emit_operation(
            "flux.post", stream,
            f"post event to Flux stream {stream!r}: {text}",
        )
        if not _get_verdict():
            _error_result([f"flux post denied: {stream}"])
            return
        try:
            base, token = _flux_env()
            lines = handle_post(base, token, stream, text)
        except Exception as e:
            _error_result([f"Flux post failed: {e}"])
            return
        _ok_result("\n".join(lines))
        return

    # -- send <stream> <text...> -------------------------------------------
    if lowered.startswith("send ") or lowered == "send":
        if len(parts) < 3 or not parts[1].strip() or not parts[2].strip():
            _error_result(["usage: send <stream> <text...>"])
            return
        stream = parts[1].strip()
        text = parts[2].strip()
        print(f'KYREX_PROGRESS:{{"flux": {json.dumps(lowered)}}}', flush=True)
        _emit_operation(
            "flux.send", stream,
            f"send event on Flux stream {stream!r} to external subscribers: {text}",
        )
        if not _get_verdict():
            _error_result([f"flux send denied: {stream}"])
            return
        try:
            base, token = _flux_env()
            lines = handle_send(base, token, stream, text)
        except Exception as e:
            _error_result([f"Flux send failed: {e}"])
            return
        _ok_result("\n".join(lines))
        return

    # -- unsupported -------------------------------------------------------
    _error_result([
        f"unsupported flux command: {task!r} — supported: "
        "status, get <stream>, post <stream> <text>, send <stream> <text>"
    ])


if __name__ == "__main__":
    main()
