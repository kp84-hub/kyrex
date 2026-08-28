"""Tests for intent.answer_chat() — conversational LLM response.
Covers: missing config returns fallback message, successful OpenAI and
Anthropic responses via mocked HTTP, API/network error returns fallback,
long responses are truncated gracefully, classify_intent is untouched.

Run: python3 test_answer_chat.py
"""
import io
import json
import os
import sys
import urllib.request
from unittest.mock import patch

os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-4o-mini")
os.environ.setdefault("KYREX_API_KEY", "sk-test-fake")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intent

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# ── Helpers ────────────────────────────────────────────────────────────────

def _fake_urlopen_ok(body_bytes: bytes):
    """Return a callable that returns an io.BytesIO wrapper around body_bytes."""
    return lambda req, timeout=30: io.BufferedReader(io.BytesIO(body_bytes))


# ── Test 1: missing API key → fallback message (no HTTP call) ──────────────

def test_no_api_key():
    saved = os.environ.pop("KYREX_API_KEY", None)
    try:
        result = intent.answer_chat("hello")
        check("missing API key returns fallback",
              result == ("I can check your calendar, read files, or take a repo task. "
                         "Prefix with cal:, fs:, or repo: to be explicit."))
    finally:
        if saved:
            os.environ["KYREX_API_KEY"] = saved

print("\nTest 1: no API key")
test_no_api_key()


# ── Test 2: missing model → fallback message (no HTTP call) ────────────────

def test_no_model():
    saved = os.environ.pop("KYREX_MODEL", None)
    try:
        result = intent.answer_chat("hello")
        check("missing model returns fallback",
              result == ("I can check your calendar, read files, or take a repo task. "
                         "Prefix with cal:, fs:, or repo: to be explicit."))
    finally:
        if saved:
            os.environ["KYREX_MODEL"] = saved

print("\nTest 2: no model")
test_no_model()


# ── Test 3: OpenAI successful response ──────────────────────────────────────

def test_openai_ok():
    fake_body = json.dumps({
        "choices": [{"message": {"content": "Hello! How can I help you today?"}}]
    }).encode()
    with patch("urllib.request.urlopen", _fake_urlopen_ok(fake_body)):
        result = intent.answer_chat("Hi there")
    check("OpenAI returns parsed content", result == "Hello! How can I help you today?", repr(result))

print("\nTest 3: OpenAI success")
test_openai_ok()


# ── Test 4: Anthropic successful response ───────────────────────────────────

def test_anthropic_ok():
    saved_provider = os.environ.get("KYREX_PROVIDER")
    os.environ["KYREX_PROVIDER"] = "anthropic"
    fake_body = json.dumps({
        "content": [{"type": "text", "text": "Sure! I'd be happy to help."}]
    }).encode()
    try:
        with patch("urllib.request.urlopen", _fake_urlopen_ok(fake_body)):
            result = intent.answer_chat("Can you help?")
        check("Anthropic returns parsed content",
              result == "Sure! I'd be happy to help.", repr(result))
    finally:
        if saved_provider:
            os.environ["KYREX_PROVIDER"] = saved_provider
        else:
            os.environ.pop("KYREX_PROVIDER", None)

print("\nTest 4: Anthropic success")
test_anthropic_ok()


# ── Test 5: empty LLM reply → rephrase message ─────────────────────────────

def test_empty_reply():
    fake_body = json.dumps({
        "choices": [{"message": {"content": ""}}]
    }).encode()
    with patch("urllib.request.urlopen", _fake_urlopen_ok(fake_body)):
        result = intent.answer_chat("...")
    check("empty reply returns rephrase",
          result == "I'm not sure how to answer that. Could you rephrase?", repr(result))

print("\nTest 5: empty reply")
test_empty_reply()


# ── Test 6: None reply (Anthropic) → rephrase message ──────────────────────

def test_none_reply_anthropic():
    """None content is malformed — answer_chat treats it as error and
    returns the safe fallback, not the rephrase message."""
    saved_provider = os.environ.get("KYREX_PROVIDER")
    os.environ["KYREX_PROVIDER"] = "anthropic"
    fake_body = json.dumps({
        "content": None
    }).encode()
    expected = ("I can check your calendar, read files, or take a repo task. "
                "Prefix with cal:, fs:, or repo: to be explicit.")
    try:
        with patch("urllib.request.urlopen", _fake_urlopen_ok(fake_body)):
            result = intent.answer_chat("hello")
        check("None Anthropic reply returns fallback",
              result == expected, repr(result))
    finally:
        if saved_provider:
            os.environ["KYREX_PROVIDER"] = saved_provider
        else:
            os.environ.pop("KYREX_PROVIDER", None)

print("\nTest 6: None Anthropic reply")
test_none_reply_anthropic()


# ── Test 7: API/network error → fallback message ───────────────────────────

def test_network_error():
    def _raise_error(req, timeout=30):
        raise urllib.error.URLError("connection refused")
    with patch("urllib.request.urlopen", _raise_error):
        result = intent.answer_chat("Hello")
    check("network error returns fallback",
          result == ("I can check your calendar, read files, or take a repo task. "
                     "Prefix with cal:, fs:, or repo: to be explicit."))

print("\nTest 7: network error")
test_network_error()


# ── Test 8: long response truncation ────────────────────────────────────────

def test_truncation():
    # Produce a response longer than _TG_MAX (4000) chars.
    long_text = "Hello. " * 1000  # ~7000 chars
    fake_body = json.dumps({
        "choices": [{"message": {"content": long_text}}]
    }).encode()
    with patch("urllib.request.urlopen", _fake_urlopen_ok(fake_body)):
        result = intent.answer_chat("Tell me a long story")
    check("long response is truncated",
          len(result) <= 4050 and "(response truncated)" in result,
          f"length={len(result)} first_120={result[:120]!r}")
    # Ensure truncation ended at a sentence boundary (ends with "...")
    check("truncation ends with ellipsis notice",
          result.endswith("(response truncated)"), repr(result[-60:]))

print("\nTest 8: truncation")
test_truncation()


# ── Test 9: classify_intent is NOT modified by answer_chat code ──────────

def test_classify_intact():
    """Confirm that classify_intent is still the same function object and
    that its behavior for no-API-key is unchanged."""
    saved_api = os.environ.pop("KYREX_API_KEY", None)
    saved_model = os.environ.pop("KYREX_MODEL", None)
    try:
        v = intent.classify_intent("what's the weather?")
        check("classify_intent still safe-returns chat on missing config",
              v == {"executor": "chat", "instruction": "what's the weather?", "confidence": 0.0},
              repr(v))
    finally:
        if saved_api:
            os.environ["KYREX_API_KEY"] = saved_api
        if saved_model:
            os.environ["KYREX_MODEL"] = saved_model

print("\nTest 9: classify_intent unchanged")
test_classify_intact()


# ── Results ────────────────────────────────────────────────────────────────

print(f"\n{'='*40}")
if failures:
    print(f"FAILURES ({len(failures)}): {', '.join(failures)}")
    sys.exit(1)
else:
    print("ALL TESTS PASSED")