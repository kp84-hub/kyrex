"""Deterministic tests for the isolated TypeSafe Jev decision client.

No test hits the live API. HTTP is simulated via the JevClient transport
seam. A live smoke test runs only when TYPESAFE_API_KEY is present.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kyrex.decision import (
    JevClient,
    JevError,
    RISK_QUESTION,
    format_decision,
)


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body
        self.text = text or (str(json_body) if json_body is not None else "")

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def make_transport(responses, calls):
    """Return a transport yielding the given responses in order."""
    it = iter(responses)

    def transport(method, url, payload, headers, timeout):
        calls.append({"method": method, "url": url, "payload": payload, "headers": headers, "timeout": timeout})
        return next(it)

    return transport


CHOICE_BODY = {
    "model": "jev-1.13.0",
    "answers": {
        "risk": {
            "type": "choice",
            "choice": "medium",
            "confidence": 0.34,
            "probabilities": {"medium": 0.56, "low": 0.01, "high": 0.43},
        }
    },
    "usage": {"input_tokens": 365, "output_tokens": 38},
}


# ── success shapes ───────────────────────────────────────────────

def test_choice_response_parsed():
    calls = []
    t = make_transport([FakeResponse(200, CHOICE_BODY)], calls)
    c = JevClient(api_key="k", transport=t)
    r = c.decide("state", {"risk": RISK_QUESTION})
    assert r["model"] == "jev-1.13.0"
    ans = r["answers"]["risk"]
    assert ans["choice"] == "medium"
    assert ans["confidence"] == 0.34
    assert ans["probabilities"]["medium"] == 0.56
    assert ans["probabilities"]["low"] == 0.01
    assert r["usage"]["input_tokens"] == 365


def test_noul_response_parsed():
    body = {
        "model": "jev-1.13.0",
        "answers": {"q": {"type": "noul", "probability": 0.87}},
        "usage": {},
    }
    calls = []
    c = JevClient(api_key="k", transport=make_transport([FakeResponse(200, body)], calls))
    r = c.decide("state", {"q": {"type": "noul"}})
    assert r["answers"]["q"]["probability"] == 0.87


def test_score_response_parsed():
    body = {
        "model": "jev-1.13.0",
        "answers": {
            "q": {
                "type": "score",
                "score": 0.72,
                "legend": {"0": "bad", "1": "good"},
                "probabilities": {"0": 0.28, "1": 0.72},
                "confidence": 0.5,
            }
        },
        "usage": {},
    }
    calls = []
    c = JevClient(api_key="k", transport=make_transport([FakeResponse(200, body)], calls))
    r = c.decide("state", {"q": {"type": "score"}})
    assert r["answers"]["q"]["score"] == 0.72


# ── request construction ─────────────────────────────────────────

def test_bearer_auth_and_url():
    calls = []
    c = JevClient(api_key="sekrit", transport=make_transport([FakeResponse(200, CHOICE_BODY)], calls))
    c.decide("s", {"risk": RISK_QUESTION})
    h = calls[0]["headers"]
    assert h["Authorization"] == "Bearer sekrit"
    assert h["Content-Type"] == "application/json"
    assert calls[0]["url"] == "https://api.typesafe.ai/v1/systemone"


def test_default_model_jev_latest():
    calls = []
    c = JevClient(api_key="k", transport=make_transport([FakeResponse(200, CHOICE_BODY)], calls))
    c.decide("s", {"risk": RISK_QUESTION})
    assert calls[0]["payload"]["model"] == "jev-latest"


def test_state_string_preserved():
    calls = []
    c = JevClient(api_key="k", transport=make_transport([FakeResponse(200, CHOICE_BODY)], calls))
    c.decide("a string state", {"risk": RISK_QUESTION})
    assert calls[0]["payload"]["state"] == "a string state"


def test_state_object_and_array_preserved():
    calls = []
    obj = {"action": "delete", "path": "x.py"}
    arr = ["delete", "x.py"]
    c = JevClient(api_key="k", transport=make_transport([FakeResponse(200, CHOICE_BODY)] * 2, calls))
    c.decide(obj, {"risk": RISK_QUESTION})
    c.decide(arr, {"risk": RISK_QUESTION})
    assert calls[0]["payload"]["state"] == obj
    assert calls[1]["payload"]["state"] == arr


def test_answer_key_preservation():
    body = {
        "model": "m",
        "answers": {"weird_key_9": {"type": "noul", "probability": 0.1}},
        "usage": {},
    }
    calls = []
    c = JevClient(api_key="k", transport=make_transport([FakeResponse(200, body)], calls))
    r = c.decide("s", {"weird_key_9": {"type": "noul"}})
    assert "weird_key_9" in r["answers"]


def test_explicit_timeout_used():
    calls = []
    c = JevClient(api_key="k", timeout=7.5, transport=make_transport([FakeResponse(200, CHOICE_BODY)], calls))
    c.decide("s", {"risk": RISK_QUESTION})
    assert calls[0]["timeout"] == 7.5


# ── fail closed ──────────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    "not an object",
    {"answers": {}},
    {"model": "m"},
    {"model": 5, "answers": {"q": {"type": "noul", "probability": 1}}},
    {"model": "m", "answers": {"q": {"type": "unknown"}}},
    {"model": "m", "answers": {"q": {"type": "choice"}}},          # no choice
    {"model": "m", "answers": {"q": {"type": "noul"}}},           # no probability
    {"model": "m", "answers": {"q": {"type": "score"}}},          # no score
    {"model": "m", "answers": {"q": "flat"}, "usage": {}},
    {"model": "m", "answers": {"q": {"type": "noul", "probability": 1}}, "usage": "bad"},
])
def test_malformed_responses_fail_closed(body):
    calls = []
    c = JevClient(api_key="k", transport=make_transport([FakeResponse(200, body)], calls))
    with pytest.raises(JevError):
        c.decide("s", {"risk": RISK_QUESTION})


def test_non_json_body_fails_closed():
    calls = []
    c = JevClient(api_key="k", transport=make_transport([FakeResponse(200, None, text="<html>")], calls))
    with pytest.raises(JevError):
        c.decide("s", {"risk": RISK_QUESTION})


def test_missing_api_key_fails_clearly(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(JevError) as ei:
        JevClient()
    assert "TYPESAFE_API_KEY" in str(ei.value)
    assert "KYREX_API_KEY" not in str(ei.value)


def test_key_never_in_error_or_report_output():
    calls = []
    c = JevClient(api_key="TOPSECRETKEY", transport=make_transport([FakeResponse(401, {"e": "no"}, text="no")], calls))
    with pytest.raises(JevError) as ei:
        c.decide("s", {"risk": RISK_QUESTION})
    assert "TOPSECRETKEY" not in str(ei.value)
    out = format_decision(CHOICE_BODY, "risk")
    assert "TOPSECRETKEY" not in out


# ── retry behavior ───────────────────────────────────────────────

def test_401_no_retry():
    calls = []
    sleeps = []
    c = JevClient(api_key="k", transport=make_transport([FakeResponse(401, {}, text="x")] * 5, calls), sleep=sleeps.append)
    with pytest.raises(JevError):
        c.decide("s", {"risk": RISK_QUESTION})
    assert len(calls) == 1
    assert sleeps == []


def test_422_no_retry():
    calls = []
    sleeps = []
    c = JevClient(api_key="k", transport=make_transport([FakeResponse(422, {}, text="bad")] * 5, calls), sleep=sleeps.append)
    with pytest.raises(JevError):
        c.decide("s", {"risk": RISK_QUESTION})
    assert len(calls) == 1
    assert sleeps == []


def test_429_bounded_retry_then_success():
    calls = []
    sleeps = []
    c = JevClient(
        api_key="k",
        transport=make_transport([FakeResponse(429), FakeResponse(429), FakeResponse(200, CHOICE_BODY)], calls),
        sleep=sleeps.append,
    )
    r = c.decide("s", {"risk": RISK_QUESTION})
    assert len(calls) == 3
    assert r["model"] == "jev-1.13.0"
    # bounded exponential backoff
    assert sleeps == [0.5, 1.0]


def test_529_bounded_retry_then_fail():
    calls = []
    sleeps = []
    c = JevClient(
        api_key="k",
        transport=make_transport([FakeResponse(529)] * 5, calls),
        sleep=sleeps.append,
    )
    with pytest.raises(JevError):
        c.decide("s", {"risk": RISK_QUESTION})
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]


def test_timeout_raises_jev_error():
    calls = []

    def transport(method, url, payload, headers, timeout):
        calls.append(timeout)
        raise __import__("requests").Timeout("timed out")

    c = JevClient(api_key="k", transport=transport)
    with pytest.raises(JevError):
        c.decide("s", {"risk": RISK_QUESTION})


def test_network_failure_raises_jev_error():
    import requests as _requests

    def transport(method, url, payload, headers, timeout):
        raise _requests.ConnectionError("refused")

    c = JevClient(api_key="k", transport=transport)
    with pytest.raises(JevError) as ei:
        c.decide("s", {"risk": RISK_QUESTION})
    assert "TOPSECRET" not in str(ei.value)


# ── rendering ────────────────────────────────────────────────────

def test_format_decision_renders_example_shape():
    out = format_decision(CHOICE_BODY, "risk")
    assert "Jev decision" in out
    assert "risk: medium" in out
    assert "confidence: 0.34" in out
    assert "model: jev-1.13.0" in out
    assert "medium: 0.56" in out


# ── isolation: existing provider selection unchanged ─────────────

def test_provider_registry_has_no_jev():
    from kyrex.providers import base as provider_base

    assert not hasattr(provider_base, "Jev")
    src = Path(provider_base.__file__).read_text()
    assert "jev" not in src.lower()
    assert "typesafe" not in src.lower()


def test_config_module_has_no_typesafe_coupling():
    import kyrex.config as cfgmod

    src = Path(cfgmod.__file__).read_text()
    assert "typesafe" not in src.lower()
    assert "TYPESAFE_API_KEY" not in src


# ── CLI boundary ─────────────────────────────────────────────────

def test_cli_decision_dispatches_before_setup_gate(monkeypatch, capsys):
    """`kyrex decision ...` must never reach setup/config/engine paths."""
    import kyrex.cli as cli

    def boom(*a, **k):
        raise AssertionError("setup wizard must not run for decision command")

    monkeypatch.setattr(cli.ConfigManager, "setup_wizard", boom)

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def decide(self, state, questions):
            assert questions["risk"]["type"] == "choice"
            return CHOICE_BODY

    monkeypatch.setattr("kyrex.decision.JevClient", FakeClient)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    cli._run_decision_command(["test", "--question", "risk", "--state", "s"])
    out = capsys.readouterr().out
    assert "risk: medium" in out


def test_cli_rejects_unknown_question(monkeypatch, capsys):
    import kyrex.cli as cli

    cli._run_decision_command(["test", "--question", "hax", "--state", "s"])
    out = capsys.readouterr().out
    assert "only the 'risk' question" in out.lower()


def test_cli_unsupported_question_never_calls_jev(monkeypatch):
    import kyrex.cli as cli

    def boom(*a, **k):
        raise AssertionError("JevClient must not be constructed")

    monkeypatch.setattr("kyrex.decision.JevClient", boom)
    cli._run_decision_command(["test", "--question", "hax", "--state", "s"])


def test_bool_confidence_fails_closed():
    calls = []
    body = {
        "model": "m",
        "answers": {"q": {"type": "choice", "choice": "a", "confidence": True, "probabilities": {"a": 1.0}}},
    }
    c = JevClient(api_key="k", transport=make_transport([FakeResponse(200, body)], calls))
    with pytest.raises(JevError):
        c.decide("s", {"q": {"type": "choice"}})


def test_score_bad_probabilities_fails_closed():
    calls = []
    body = {"model": "m", "answers": {"q": {"type": "score", "score": 0.5, "probabilities": "bad"}}}
    c = JevClient(api_key="k", transport=make_transport([FakeResponse(200, body)], calls))
    with pytest.raises(JevError):
        c.decide("s", {"q": {"type": "score"}})


def test_bool_probability_fails_closed():
    calls = []
    body = {"model": "m", "answers": {"q": {"type": "noul", "probability": True}}}
    c = JevClient(api_key="k", transport=make_transport([FakeResponse(200, body)], calls))
    with pytest.raises(JevError):
        c.decide("s", {"q": {"type": "noul"}})


def test_choice_missing_probabilities_fails_closed():
    calls = []
    body = {"model": "m", "answers": {"q": {"type": "choice", "choice": "a", "confidence": 0.5}}}
    c = JevClient(api_key="k", transport=make_transport([FakeResponse(200, body)], calls))
    with pytest.raises(JevError):
        c.decide("s", {"q": {"type": "choice"}})


# ── opt-in live smoke (never prints the key) ─────────────────────

@pytest.mark.skipif(
    not os.environ.get("TYPESAFE_API_KEY"),
    reason="TYPESAFE_API_KEY not set; live smoke test is opt-in",
)
def test_live_smoke_risk_question():
    from kyrex.decision import RISK_QUESTION

    client = JevClient()
    result = client.decide(
        "A coding agent wants to delete an existing source file.",
        {"risk": RISK_QUESTION},
    )
    assert isinstance(result["model"], str)
    assert "risk" in result["answers"]
    rendered = format_decision(result, "risk")
    key = os.environ["TYPESAFE_API_KEY"]
    assert key not in rendered
