"""Bounded Jev routing authority for Kyrex Chat.

Jev is a routing control plane only. It may select from host-supplied legal
execution routes and, for coordinator turns, from a host-supplied safe Bot
roster. Kyrex remains authoritative for reasoning, capability/policy checks,
request parsing, approvals, tool execution, retries/fallback, and user-facing
output.

Every Jev decision fails back to the caller's deterministic route/target on
transport errors, malformed answers, low confidence, or unavailable choices.
Routing telemetry is metadata-only and never persists the request text.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from kyrex.decision import JevClient, JevError, RISK_QUESTION

_TRUE = frozenset({"1", "true", "yes", "on"})

ROUTE_DESCRIPTIONS = {
    "engine": "Conversational/read-only Kyrex engine; no durable external write action.",
    "repo": "Developer executor for repository edits, commands, tests, and code changes.",
    "browser": "Read-only Browser Bot navigation and page reading through an allowed host.",
    "calendar": "Read-only calendar lookup/listing.",
    "calendar_write": "Create or schedule a calendar event through the Calendar Writer.",
    "calendar_delete": "Delete a calendar event through the Calendar Editor; destructive and T2-gated.",
}

# Used only to prevent a review-required Jev recommendation from increasing
# authority relative to the deterministic route. It never grants authority.
_ROUTE_IMPACT = {
    "engine": 0,
    "calendar": 0,
    "browser": 1,
    "repo": 2,
    "calendar_write": 2,
    "calendar_delete": 3,
}

REVIEW_QUESTION = {
    "type": "noul",
    "instructions": (
        "This request should receive explicit human review before Kyrex moves "
        "to a more privileged or side-effecting execution route."
    ),
}


def enabled_from_env() -> bool:
    return (
        os.environ.get("KYREX_JEV_ROUTING", "").strip().lower() in _TRUE
        and bool(os.environ.get("TYPESAFE_API_KEY", "").strip())
    )


def _float_env(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _log_path() -> Path:
    configured = os.environ.get("KYREX_JEV_ROUTING_LOG", "").strip()
    if configured:
        return Path(configured).expanduser()
    data_dir = os.environ.get("KYREX_DATA_DIR", "").strip()
    root = Path(data_dir).expanduser() if data_dir else Path.home() / ".kyrex"
    return root / "jev_routing.jsonl"


def _append(record: dict) -> None:
    """Best-effort metadata-only routing telemetry; never blocks routing."""
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


def _result(
    *,
    fallback: str,
    route: str | None = None,
    source: str,
    reason: str,
    jev_route: str | None = None,
    confidence: float | None = None,
    risk: str | None = None,
    needs_review: float | None = None,
    model: str | None = None,
    available_routes: Iterable[str] = (),
) -> dict:
    selected = route or fallback
    record = {
        "kind": "execution_route",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "selected_route": selected,
        "fallback_route": fallback,
        "jev_route": jev_route,
        "source": source,
        "reason": reason,
        "confidence": confidence,
        "risk": risk,
        "needs_review": needs_review,
        "model": model,
        "available_routes": sorted(set(available_routes)),
    }
    _append(record)
    return record


def decide_route(
    request_text: str,
    available_routes: Iterable[str],
    fallback_route: str,
    *,
    client: JevClient | None = None,
    enabled: bool | None = None,
) -> dict:
    """Return the active Kyrex execution route plus Jev decision metadata.

    ``available_routes`` MUST already be derived from authoritative Kyrex Bot
    capability/readiness checks. Jev cannot introduce a route that is absent
    from this set. The full request text is sent to Jev only when active routing
    is explicitly enabled; routing telemetry never persists that text.
    """
    routes = tuple(sorted({
        str(route) for route in available_routes
        if str(route) in ROUTE_DESCRIPTIONS
    }))
    fallback = str(fallback_route)
    if fallback not in routes:
        routes = tuple(sorted(set(routes) | {fallback}))

    is_enabled = enabled_from_env() if enabled is None else bool(enabled)
    if not is_enabled:
        return _result(
            fallback=fallback, source="deterministic", reason="jev_disabled",
            available_routes=routes)
    if len(routes) <= 1:
        return _result(
            fallback=fallback, source="deterministic", reason="single_route",
            available_routes=routes)

    questions = {
        "route": {
            "type": "choice",
            "instructions": (
                "Choose the best Kyrex execution route for this user request. "
                "Choose only from the provided legal routes."
            ),
            "criteria": {route: ROUTE_DESCRIPTIONS[route] for route in routes},
        },
        "risk": RISK_QUESTION,
        "needs_review": REVIEW_QUESTION,
    }
    state = {
        "request": str(request_text or "")[:4000],
        "surface": "Kyrex Chat",
        "available_routes": list(routes),
    }

    try:
        active_client = client or JevClient(
            timeout=_float_env("KYREX_JEV_ROUTING_TIMEOUT", 3.0, 0.5, 10.0))
        decision = active_client.decide(state, questions)
    except JevError:
        return _result(
            fallback=fallback, source="deterministic", reason="jev_error",
            available_routes=routes)
    except Exception:
        return _result(
            fallback=fallback, source="deterministic", reason="jev_exception",
            available_routes=routes)

    answers = decision.get("answers") or {}
    route_answer = answers.get("route") or {}
    risk_answer = answers.get("risk") or {}
    review_answer = answers.get("needs_review") or {}

    jev_route = str(route_answer.get("choice") or "")
    confidence = route_answer.get("confidence")
    confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.0
    risk = str(risk_answer.get("choice") or "") or None
    review = review_answer.get("noul", review_answer.get("probability"))
    review = float(review) if isinstance(review, (int, float)) else None
    model = str(decision.get("model") or "") or None

    if jev_route not in routes:
        return _result(
            fallback=fallback, source="deterministic", reason="illegal_route",
            jev_route=jev_route or None, confidence=confidence, risk=risk,
            needs_review=review, model=model, available_routes=routes)

    min_conf = _float_env("KYREX_JEV_ROUTE_MIN_CONFIDENCE", 0.65, 0.0, 1.0)
    if confidence < min_conf:
        return _result(
            fallback=fallback, source="deterministic", reason="low_confidence",
            jev_route=jev_route, confidence=confidence, risk=risk,
            needs_review=review, model=model, available_routes=routes)

    review_threshold = _float_env(
        "KYREX_JEV_REVIEW_THRESHOLD", 0.65, 0.0, 1.0)
    increasing_authority = (
        _ROUTE_IMPACT.get(jev_route, 99) > _ROUTE_IMPACT.get(fallback, 99)
    )
    if (review is not None and review >= review_threshold
            and increasing_authority):
        return _result(
            fallback=fallback, source="deterministic",
            reason="review_blocks_privilege_increase", jev_route=jev_route,
            confidence=confidence, risk=risk, needs_review=review,
            model=model, available_routes=routes)

    return _result(
        fallback=fallback, route=jev_route, source="jev", reason="accepted",
        jev_route=jev_route, confidence=confidence, risk=risk,
        needs_review=review, model=model, available_routes=routes)


def _safe_text(value, limit: int = 800) -> str:
    """One-line bounded metadata for Jev criteria (never request/user secrets)."""
    return " ".join(str(value or "").split())[:limit]


def _bot_result(
    *,
    fallback_bot_id: str,
    selected_bot_id: str | None = None,
    source: str,
    reason: str,
    jev_bot_id: str | None = None,
    confidence: float | None = None,
    model: str | None = None,
    available_bot_ids: Iterable[str] = (),
    shared_tools: Iterable[str] = (),
) -> dict:
    selected = selected_bot_id or fallback_bot_id
    record = {
        "kind": "bot_route",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "selected_bot_id": selected,
        "fallback_bot_id": fallback_bot_id,
        "jev_bot_id": jev_bot_id,
        "source": source,
        "reason": reason,
        "confidence": confidence,
        "model": model,
        "available_bot_ids": sorted(set(available_bot_ids)),
        "shared_tools": sorted(set(str(t) for t in shared_tools if str(t))),
    }
    _append(record)
    return record


def decide_bot_target(
    request_text: str,
    candidates: Iterable[dict],
    fallback_bot_id: str,
    *,
    shared_tools: Iterable[str] = (),
    client: JevClient | None = None,
    enabled: bool | None = None,
) -> dict:
    """Choose WHICH Bot receives a coordinator turn; do not solve the task.

    This is intentionally narrower than :func:`decide_route`. Jev receives only
    the user request plus a HOST-SUPPLIED safe roster (Bot id/name/role and a
    non-secret user-facing description) and chooses one Bot id. It never sees
    provider credentials, Rift paths, policy rules, prompts, tokens, or tool
    arguments.

    ``shared_tools`` describes owner-scoped connected tools that Kyrex already
    proved available. They apply to EVERY candidate independently of Bot role;
    Jev must not use a role label as a permission check. The selected Bot does
    not gain authority: Kyrex re-runs all owner/policy/readiness/approval checks
    during execution.

    On disable/error/malformed choice/low confidence, the caller's current Bot
    remains selected. The request is sent to Jev only for the live decision and
    is NEVER written to routing telemetry.
    """
    fallback = _safe_text(fallback_bot_id, 256)
    by_id: dict[str, dict] = {}
    for raw in candidates or ():
        if not isinstance(raw, dict):
            continue
        bot_id = _safe_text(raw.get("id"), 256)
        if not bot_id or bot_id in by_id:
            continue
        by_id[bot_id] = {
            "id": bot_id,
            "name": _safe_text(raw.get("name"), 200),
            "role": _safe_text(raw.get("role"), 120),
            "description": _safe_text(raw.get("description"), 800),
        }
    if fallback and fallback not in by_id:
        by_id[fallback] = {
            "id": fallback,
            "name": fallback,
            "role": "current",
            "description": "The currently bound Kyrex Bot.",
        }

    bot_ids = tuple(sorted(by_id))
    if not fallback or fallback not in by_id:
        return _bot_result(
            fallback_bot_id=fallback or "",
            source="deterministic", reason="invalid_fallback",
            available_bot_ids=bot_ids, shared_tools=shared_tools)

    is_enabled = enabled_from_env() if enabled is None else bool(enabled)
    if not is_enabled:
        return _bot_result(
            fallback_bot_id=fallback, source="deterministic",
            reason="jev_disabled", available_bot_ids=bot_ids,
            shared_tools=shared_tools)
    if len(bot_ids) <= 1:
        return _bot_result(
            fallback_bot_id=fallback, source="deterministic",
            reason="single_bot", available_bot_ids=bot_ids,
            shared_tools=shared_tools)

    criteria = {}
    for bot_id in bot_ids:
        item = by_id[bot_id]
        parts = [
            f"Name: {item['name'] or bot_id}.",
            f"Role/persona: {item['role'] or 'custom'}.",
        ]
        if item["description"]:
            parts.append(item["description"])
        criteria[bot_id] = " ".join(parts)[:1200]

    tools = sorted(set(str(t) for t in shared_tools if str(t)))
    questions = {
        "target": {
            "type": "choice",
            "instructions": (
                "ROUTING ONLY. Choose which Kyrex Bot should receive this user "
                "request. Do not solve, plan, summarize, extract facts, compose "
                "a response, or choose tool arguments. Bot role/persona is a "
                "routing signal, NOT a permission boundary. Owner-connected "
                "tools listed in state.shared_tools are available to every Bot "
                "and Kyrex independently enforces their operation gates. Choose "
                "only one of the host-provided Bot ids."
            ),
            "criteria": criteria,
        },
    }
    state = {
        "request": str(request_text or "")[:4000],
        "surface": "Kyrex Chat",
        "routing_only": True,
        "available_bot_ids": list(bot_ids),
        "shared_tools": tools,
    }

    try:
        active_client = client or JevClient(
            timeout=_float_env("KYREX_JEV_ROUTING_TIMEOUT", 3.0, 0.5, 10.0))
        decision = active_client.decide(state, questions)
    except JevError:
        return _bot_result(
            fallback_bot_id=fallback, source="deterministic",
            reason="jev_error", available_bot_ids=bot_ids, shared_tools=tools)
    except Exception:
        return _bot_result(
            fallback_bot_id=fallback, source="deterministic",
            reason="jev_exception", available_bot_ids=bot_ids, shared_tools=tools)

    answer = ((decision.get("answers") or {}).get("target") or {})
    jev_bot_id = _safe_text(answer.get("choice"), 256)
    confidence = answer.get("confidence")
    confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.0
    model = _safe_text(decision.get("model"), 200) or None

    if jev_bot_id not in by_id:
        return _bot_result(
            fallback_bot_id=fallback, source="deterministic",
            reason="illegal_bot", jev_bot_id=jev_bot_id or None,
            confidence=confidence, model=model, available_bot_ids=bot_ids,
            shared_tools=tools)

    min_conf = _float_env(
        "KYREX_JEV_BOT_MIN_CONFIDENCE",
        _float_env("KYREX_JEV_ROUTE_MIN_CONFIDENCE", 0.65, 0.0, 1.0),
        0.0, 1.0)
    if confidence < min_conf:
        return _bot_result(
            fallback_bot_id=fallback, source="deterministic",
            reason="low_confidence", jev_bot_id=jev_bot_id,
            confidence=confidence, model=model, available_bot_ids=bot_ids,
            shared_tools=tools)

    return _bot_result(
        fallback_bot_id=fallback, selected_bot_id=jev_bot_id,
        source="jev", reason="accepted", jev_bot_id=jev_bot_id,
        confidence=confidence, model=model, available_bot_ids=bot_ids,
        shared_tools=tools)
