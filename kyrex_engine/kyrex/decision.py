"""TypeSafe Jev decision-model client — isolated from the generative LLM stack.

Jev is NOT a Kyrex chat/provider LLM. This module is a standalone client for
TypeSafe's systemone decision endpoint. Nothing in the provider, approval,
Bot, or tool-execution layers imports it, and no Jev result may approve,
deny, or execute anything in this slice.

Uses the existing ``requests`` dependency. Authenticates with a dedicated
``TYPESAFE_API_KEY`` environment variable — never a generative provider's
secret. The key is never logged, returned, or embedded in exceptions.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

SYSTEMONE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3          # total attempts for retryable statuses
BASE_DELAY = 0.5         # seconds; bounded exponential backoff for 429/529
MAX_DELAY = 8.0

# Retry ONLY on these documented transient statuses. 401/422 never retry.
_RETRYABLE_STATUSES = (429, 529)


class JevError(RuntimeError):
    """Raised for any Jev call failure. Never carries the API key."""


def _get_api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        raise JevError(
            "TYPESAFE_API_KEY is not set. Export it to use the Jev "
            "decision model. (Generative provider keys are not used.)"
        )
    return key


class JevClient:
    """Minimal native TypeSafe systemone client.

    ``transport`` is injectable for deterministic tests: a callable
    ``(method, url, json_payload, headers, timeout) -> requests.Response``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = SYSTEMONE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        transport=None,
        sleep=time.sleep,
    ):
        self._api_key = api_key if api_key is not None else _get_api_key()
        self._base_url = base_url
        self._model = model
        self._timeout = timeout
        self._transport = transport
        self._sleep = sleep

    # ── transport ────────────────────────────────────────────────
    def _post(self, payload: dict) -> requests.Response:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._transport is not None:
            return self._transport("POST", self._base_url, payload, headers, self._timeout)
        return requests.post(
            self._base_url, json=payload, headers=headers, timeout=self._timeout
        )

    def _post_with_retry(self, payload: dict) -> requests.Response:
        delay = BASE_DELAY
        last_resp = None
        for attempt in range(MAX_RETRIES):
            resp = self._post(payload)
            if resp.status_code not in _RETRYABLE_STATUSES:
                return resp
            last_resp = resp
            if attempt < MAX_RETRIES - 1:
                self._sleep(delay)
                delay = min(delay * 2, MAX_DELAY)
        return last_resp

    # ── public API ───────────────────────────────────────────────
    def decide(self, state: Any, questions: dict) -> dict:
        """Call systemone. ``state`` may be str, object, or array.

        Returns the parsed structured result:
        ``{"model": str, "answers": {name: {...}}, "usage": {...}}``.
        Fails closed (JevError) on malformed/unexpected structures.
        """
        if not isinstance(questions, dict) or not questions:
            raise JevError("questions must be a non-empty map")
        payload = {"state": state, "model": self._model, "questions": questions}

        try:
            resp = self._post_with_retry(payload)
        except requests.Timeout as e:
            raise JevError(f"Jev request timed out after {self._timeout}s") from e
        except requests.RequestException as e:
            # Raise only the exception class name — exception text may embed
            # request details, and we never risk leaking transport context.
            raise JevError(f"Jev request failed: {type(e).__name__}") from e

        status = resp.status_code
        if status == 401:
            raise JevError("Jev authentication failed (401): missing or invalid TYPESAFE_API_KEY.")
        if status == 422:
            # Schema rejection: keep the message generic. Response bodies from
            # an unexpected endpoint could echo request content.
            raise JevError("Jev rejected the request (422): invalid questions/state schema")
        if status in _RETRYABLE_STATUSES:
            raise JevError(f"Jev overloaded (HTTP {status}) after {MAX_RETRIES} attempts.")
        if status != 200:
            # Deliberately no response body in the message: unknown endpoints
            # or proxies could echo request content (including auth headers).
            raise JevError(f"Jev unexpected HTTP status {status}")

        try:
            body = resp.json()
        except ValueError as e:
            raise JevError("Jev returned a non-JSON response") from e
        return _parse_result(body)


def _parse_result(body: Any) -> dict:
    """Fail-closed structural validation of a systemone response."""
    if not isinstance(body, dict):
        raise JevError("Jev response is not a JSON object")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise JevError("Jev response missing 'model' string")
    answers = body.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise JevError("Jev response missing 'answers' map")
    for name, ans in answers.items():
        _validate_answer(name, ans)
    usage = body.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise JevError("Jev response 'usage' must be an object")
    return {"model": model, "answers": answers, "usage": usage or {}}


def _validate_answer(name: str, ans: Any) -> None:
    if not isinstance(ans, dict):
        raise JevError(f"Jev answer '{name}' is not an object")
    qtype = ans.get("type")
    if qtype == "noul":
        # The public API uses ``noul``. Accept the earlier ``probability``
        # spelling as well so stored fixtures and early-access responses remain
        # readable during the transition.
        prob = ans.get("noul", ans.get("probability"))
        if not isinstance(prob, (int, float)) or isinstance(prob, bool):
            raise JevError(f"Jev noul answer '{name}' missing numeric 'noul'")
    elif qtype == "choice":
        if not isinstance(ans.get("choice"), str):
            raise JevError(f"Jev choice answer '{name}' missing string 'choice'")
        probs = ans.get("probabilities")
        if not isinstance(probs, dict) or not all(
            isinstance(v, (int, float)) for v in probs.values()
        ):
            raise JevError(f"Jev choice answer '{name}' missing valid 'probabilities' map")
        conf = ans.get("confidence")
        if not isinstance(conf, (int, float)):
            raise JevError(f"Jev choice answer '{name}' missing numeric 'confidence'")
    elif qtype == "score":
        score = ans.get("score")
        if not isinstance(score, (int, float)):
            raise JevError(f"Jev score answer '{name}' missing numeric 'score'")
        probs = ans.get("probabilities")
        if probs is not None and not isinstance(probs, dict):
            raise JevError(f"Jev score answer '{name}' has invalid 'probabilities'")
    else:
        raise JevError(f"Jev answer '{name}' has unsupported type {qtype!r}")

    # bool is an int subclass; reject it wherever a number is expected
    for field in ("noul", "probability", "confidence", "score"):
        if field in ans and isinstance(ans[field], bool):
            raise JevError(f"Jev answer '{name}' has boolean '{field}'")


# ── Initial narrow risk question (documented schema) ─────────────
RISK_QUESTION = {
    "type": "choice",
    "instructions": "Classify the risk of this proposed coding-agent action.",
    "criteria": {
        "low": "Routine and easily reversible action with minimal impact",
        "medium": "Action that may change local data or cause limited external side effects",
        "high": "Destructive, privileged, broadly external, or difficult-to-reverse action",
    },
}


def format_decision(result: dict, question_name: str) -> str:
    """Human-readable rendering. Never includes the API key (it never enters result)."""
    lines = ["Jev decision"]
    ans = result["answers"].get(question_name)
    if ans is None:
        lines.append(f"{question_name}: <no answer returned>")
    elif ans.get("type") == "choice":
        lines.append(f"{question_name}: {ans.get('choice')}")
        lines.append(f"confidence: {ans.get('confidence')}")
        lines.append("probabilities:")
        for opt, p in sorted(
            (ans.get("probabilities") or {}).items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"  {opt}: {p}")
    elif ans.get("type") == "noul":
        lines.append(
            f"{question_name}: {ans.get('noul', ans.get('probability'))}"
        )
    elif ans.get("type") == "score":
        lines.append(f"{question_name} score: {ans.get('score')}")
    lines.append(f"model: {result['model']}")
    return "\n".join(lines)
