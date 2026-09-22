"""Bounded Jev routing authority for Kyrex Chat.

Jev may recommend a route only from a host-supplied allowlist of routes that
Kyrex already proved legal for the selected Bot. Kyrex remains authoritative
for capability/policy checks, request parsing, approval tiers, and execution.

This module deliberately fails back to the caller's deterministic route on
transport errors, malformed answers, low confidence, unavailable routes, or a
review-required recommendation that would increase execution authority.
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
    """Return the active Kyrex route plus Jev decision metadata.

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
    except JevError as exc:
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
