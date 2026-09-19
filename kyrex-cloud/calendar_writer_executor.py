#!/usr/bin/env python3
"""calendar_writer_executor.py — the Calendar WRITER executor.

Creates ONE event on the OWNER's PRIMARY calendar (America/New_York), through
the owner's own event-write authorization, and NOTHING else. Distinct from the
read-only Calendar Reader (cal_executor.py).

Flow (each step fails closed):

  1. Resolve the create intent from ``--task`` (a JSON intent, or the owner's
     raw request text) and validate it against the bounded field set.
  2. Require an owner-scoped Bot identity (``KYREX_BOT_OWNER``).
  3. Announce ``cal.create`` (tier 0) so the host classifies the operation and
     consume the host's decision.
  4. MANDATORY CONFIRMATION GATE: raise ``KYREX_APPROVAL`` carrying the EXACT
     event payload and block until the owner explicitly approves. A deny (or a
     timeout) means NOTHING is created and NO provider call is made.
  5. Create the event via the owner's Calendar Writer connector (missing/expired
     write authorization and malformed provider responses both fail closed).
  6. Emit exactly one ``KYREX_RESULT_JSON`` with a safe, readable receipt.

Protocol: ``KYREX_PROGRESS:`` / ``KYREX_OPERATION:`` / ``KYREX_APPROVAL:`` lines
during work and exactly one ``KYREX_RESULT_JSON:`` line at the end on stdout.
"""
import argparse
import json
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import cal_writer  # noqa: E402


def _emit(kind: str, payload: dict) -> None:
    print(f"{kind}:{json.dumps(payload)}", flush=True)


def _read_decision() -> str:
    return sys.stdin.readline().strip()


def _fail(reason: str) -> int:
    _emit("KYREX_RESULT_JSON", {"status": "error", "final_response": "",
                                "errors": [reason]})
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Kyrex Cloud Calendar Writer Executor")
    ap.add_argument("--task", required=True,
                    help="a create intent (JSON) or request text")
    ap.add_argument("--repo-url", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--base", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    # 1. Normalise + validate (fail closed, no provider call).
    try:
        intent = cal_writer.intent_from_task(args.task)
    except cal_writer.CalendarWriterError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"could not read the create request: {type(exc).__name__}: {exc}")

    # 2. Owner-scoped identity.
    owner = str(os.environ.get("KYREX_BOT_OWNER") or "").strip()
    if not owner:
        return _fail("calendar writes require an owner-scoped Bot identity")

    # 3. Announce the operation; the host classifies it as cal.create (tier 0)
    #    and writes its decision back on stdin.
    _emit("KYREX_PROGRESS", {"cal_create": intent["title"]})
    _emit("KYREX_OPERATION", {
        "op": "cal.create",
        "target": intent["title"],
        "summary": f"create \u201c{intent['title']}\u201d on the owner's primary calendar",
    })
    verdict = _read_decision()
    if verdict not in ("ALLOW", "APPROVE"):
        return _fail("calendar create denied by host policy")

    # 4. Mandatory confirmation gate — the EXACT payload, then explicit owner
    #    approval, BEFORE any external call.
    _emit("KYREX_APPROVAL", {
        "tier": 1,
        "summary": cal_writer.summary_line(intent),
        "detail": cal_writer.payload_display(intent),
    })
    decision = _read_decision()
    if decision != "APPROVED":
        return _fail("calendar create not approved \u2014 nothing was created")

    # 5. The SOLE Google write call, via the owner's writer connector.
    try:
        import connectors
        store = connectors.default_store()
        created = store.calendar_writer(owner).create_event(
            cal_writer.to_google_event(intent))
    except Exception as exc:  # noqa: BLE001 — every failure fails closed
        return _fail(f"calendar create failed: {str(exc) or type(exc).__name__}")

    # 6. Safe receipt (title + when + opaque event ref). No htmlLink.
    receipt = cal_writer.format_receipt(intent, created)
    _emit("KYREX_RESULT_JSON", {"status": "ok", "final_response": receipt,
                                "errors": []})
    return 0


if __name__ == "__main__":
    main()
