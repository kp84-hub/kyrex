"""Bounded email evidence for models; never edits the owner's source email."""
from __future__ import annotations

import re

BODY_LIMIT = 8000
_REPLY_START = re.compile(r"^On .{1,400}(?:@|\b\d{4}\b).{0,200}wrote:\s*$", re.I)
_FACT_KEYS = ("title", "date", "start", "end", "all_day", "location",
              "location_ambiguous", "sender", "missing", "conflicts")


def project_email(selected: dict) -> dict:
    """Prefer an already host-selected section; otherwise bound current text.

    Keep the end as well as the beginning of a long email: event times and form
    links often occur late in newsletters. Explicitly report any omissions.
    Forwarded messages are retained; only a recognizable reply-history tail
    following nonempty current text is dropped.
    """
    headers = selected.get("headers")
    headers = headers if isinstance(headers, dict) else {}
    full = str(selected.get("body") or "")
    focus = selected.get("focus_section")
    focused = isinstance(focus, str) and bool(focus.strip())
    body = focus if focused else full
    lines = body.splitlines(keepends=True)
    omitted = False
    for index, line in enumerate(lines):
        if index and _REPLY_START.fullmatch(line.strip()) and "".join(lines[:index]).strip():
            body = "".join(lines[:index]).rstrip()
            omitted = True
            break
    truncated = len(body) > BODY_LIMIT
    if truncated:
        marker = "\n[Middle of email omitted for privacy; request a focused read for missing details.]\n"
        tail_size = 3000
        body = body[:BODY_LIMIT - tail_size - len(marker)] + marker + body[-tail_size:]
    facts = selected.get("event_facts")
    facts = facts if isinstance(facts, dict) else {}
    return {
        "headers": {key: str(headers.get(key) or "")[:500]
                    for key in ("Subject", "From", "Date")},
        "body": body,
        "content_scope": "focused_section" if focused else "current_message",
        "quoted_history_omitted": omitted,
        "body_available": bool(full.strip() or body.strip()),
        "body_truncated": bool(selected.get("truncated")) or truncated,
        "body_read_status": str(selected.get("body_read_status") or "")[:30],
        "body_reader_version": 3 if selected.get("body_reader_version") == 3 else None,
        "event_facts": {key: facts[key] for key in _FACT_KEYS if key in facts},
        "untrusted_data": True,
    }


def withhold_earlier_email(value):
    """Remove recognized earlier evidence bodies from a request copy only."""
    if isinstance(value, list):
        return [withhold_earlier_email(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: withhold_earlier_email(item) for key, item in value.items()}
    evidence = result.get("email_evidence")
    if isinstance(evidence, dict):
        result["email_evidence"] = {**evidence, "body": "", "body_withheld": True,
            "follow_up": "Earlier email body withheld. Read the selected email again if needed."}
        if "result_summary" in result:
            result["result_summary"] = "Earlier email body withheld; use retained metadata/event facts or read again."
    return result
