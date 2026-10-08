"""Inspect actual SDK payloads without making provider requests."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS

import pytest

import kyrex.providers.openai_ as openai_module
import kyrex.providers.anthropic as anthropic_module
from kyrex.providers.base import retry_with_backoff
from kyrex.providers.privacy import PRIVATE, REDACTED, SecretFilter, safe_provider_error
from kyrex.providers.email_privacy import BODY_LIMIT, project_email


class Events:
    def __init__(self, events=()):
        self.events = iter(events)
    def __aiter__(self):
        return self
    async def __anext__(self):
        try:
            return next(self.events)
        except StopIteration:
            raise StopAsyncIteration


def test_text_and_tool_history_are_filtered_without_mutating_source(monkeypatch):
    monkeypatch.setenv("GMAIL_ACCESS_TOKEN", "opaque-calendar-credential")
    messages = [
        {"role": "user", "content": "Kelly's homecoming email says Friday at 7 PM. basic advice please."},
        {"role": "assistant", "content": None, "reasoning_content": "opaque-calendar-credential",
         "tool_calls": [{"id": "call_1", "type": "function", "function": {
             "name": "mail.read", "arguments": json.dumps({"message_id": "m1", "password": "secret-login"})}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "mail.read",
         "content": json.dumps({"subject": "Homecoming", "body": "Friday 7 PM",
                                "access_token": "opaque-calendar-credential"})},
    ]
    original = copy.deepcopy(messages)
    filtered = SecretFilter().messages(messages)
    assert messages == original
    assert "Do not guess credentials" in filtered[0]["content"]
    assert filtered[1] == original[0]
    assert filtered[2]["reasoning_content"] == REDACTED
    call = filtered[2]["tool_calls"][0]
    assert call["id"] == "call_1" and call["function"]["name"] == "mail.read"
    assert json.loads(call["function"]["arguments"]) == {"message_id": "m1", "password": REDACTED}
    assert filtered[3]["tool_call_id"] == "call_1"
    assert json.loads(filtered[3]["content"]) == {"subject": "Homecoming", "body": "Friday 7 PM", "access_token": REDACTED}


@pytest.mark.parametrize("text, secret", [
    ("API_KEY='opaque-key-here'", "opaque-key-here"),
    ('refresh_token="opaque-refresh-value"', "opaque-refresh-value"),
    ("https://example.com/read?access_token=opaque-link-token&event=7", "opaque-link-token"),
    ("Authorization: Bearer opaque-bearer-credential", "opaque-bearer-credential"),
    ("-----BEGIN PRIVATE KEY-----\nprivate material\n-----END PRIVATE KEY-----", "private material"),
    ("sk-proj-abcdefghijklmnopqrstuvwx", "abcdefghijklmnopqrstuvwx"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiYWxpY2UifQ.signature", "signature"),
    ("Cookie: login=private-cookie; session=another-private-cookie", "another-private-cookie"),
    ("https://alice:private-uri-password@example.com/path", "private-uri-password"),
    ("https://example.com/file?X-Amz-Signature=private-signed-url&other=7", "private-signed-url"),
])
def test_recognizable_credentials(text, secret):
    result = SecretFilter().text(text)
    assert secret not in result and REDACTED in result


@pytest.mark.parametrize("base, model, protocol", [
    ("https://api.openai.com/v1", "test-model", "chat"),
    ("https://opencode.ai/zen/go/v1", "deepseek-v4.1-flash", "chat"),
    ("https://opencode.ai/zen/go/v1", "gpt-6-luna", "responses"),
])
def test_openai_and_go_sdk_payloads_keep_auth_separate(monkeypatch, base, model, protocol):
    captured, clients = [], []
    async def create(**kwargs):
        captured.append(kwargs)
        if protocol == "responses":
            return Events([NS(type="response.completed", response=NS(output=[], output_text="ok", usage=None))])
        return Events()
    def client(**kwargs):
        clients.append(kwargs)
        return NS(chat=NS(completions=NS(create=create)), responses=NS(create=create))
    monkeypatch.setattr(openai_module, "AsyncOpenAI", client)
    provider = openai_module.OpenAIProvider("opaque-provider-credential", base_url=base,
                 extra_headers={"X-Api-Key": "opaque-header-credential"}, session_id="session-1")
    messages = [{"role": "user", "content": "opaque-provider-credential opaque-header-credential"}]
    tools = [{"type": "function", "function": {"name": "lookup", "description": "opaque-header-credential",
              "parameters": {"type": "object", "properties": {"password": {"type": "string"}}}}}]
    result = asyncio.run(provider.chat(model, messages, tools=tools))
    assert "error" not in result
    request = captured[0]
    assert "opaque-provider-credential" not in json.dumps(request)
    assert "opaque-header-credential" not in json.dumps(request)
    assert clients[0]["api_key"] == "opaque-provider-credential"
    assert clients[0]["default_headers"]["X-Api-Key"] == "opaque-header-credential"
    assert clients[0]["default_headers"].get("x-opencode-session") == ("session-1" if "opencode.ai" in base else None)
    assert messages[0]["content"].startswith("opaque-provider")
    spec = request["tools"][0] if protocol == "responses" else request["tools"][0]["function"]
    assert spec["parameters"]["properties"]["password"] == {"type": "string"}
    if protocol == "responses" or "api.openai.com" in base:
        assert request["store"] is False
    else:
        assert "store" not in request


def test_anthropic_payload_filters_system_and_tool_results(monkeypatch):
    captured = []
    async def create(**kwargs):
        captured.append(kwargs)
        return NS(content=[NS(type="text", text="ok")], usage=None)
    monkeypatch.setattr(anthropic_module, "AsyncAnthropic", lambda **kw: NS(messages=NS(create=create)))
    provider = anthropic_module.AnthropicProvider("opaque-anthropic-credential")
    messages = [
        {"role": "system", "content": "opaque-anthropic-credential"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call1", "type": "function",
         "function": {"name": "read", "arguments": '{"access_token":"hidden-token"}'}}]},
        {"role": "tool", "tool_call_id": "call1", "content": "password=hidden-password"},
    ]
    assert asyncio.run(provider.chat("claude-test", messages))["content"] == "ok"
    text = json.dumps(captured[0])
    assert all(secret not in text for secret in ("opaque-anthropic-credential", "hidden-token", "hidden-password"))
    assert captured[0]["messages"][0]["content"][0]["id"] == "call1"


@pytest.mark.parametrize("module, cls", [(openai_module, "OpenAIProvider"), (anthropic_module, "AnthropicProvider")])
def test_provider_errors_never_echo_response_body(monkeypatch, module, cls):
    class UpstreamError(Exception):
        status_code = 429
    async def failed(**kwargs):
        raise UpstreamError("private email body, password=do-not-display")
    client = NS(chat=NS(completions=NS(create=failed)), messages=NS(create=failed))
    monkeypatch.setattr(module, "AsyncOpenAI" if cls == "OpenAIProvider" else "AsyncAnthropic", lambda **kw: client)
    result = asyncio.run(getattr(module, cls)("key").chat("test", [{"role": "user", "content": "hi"}]))
    assert "HTTP 429" in result["error"]
    assert "private email" not in str(result) and "do-not-display" not in str(result)


def test_retry_logs_only_safe_metadata(caplog):
    @retry_with_backoff(max_retries=0)
    async def fail():
        raise ValueError("private message and password=hidden")
    with pytest.raises(ValueError):
        asyncio.run(fail())
    assert "failed after 1 attempts" in caplog.text
    assert "private message" not in caplog.text and "hidden" not in caplog.text
    assert "private message" not in safe_provider_error(ValueError("private message"))


@pytest.mark.parametrize("text, secret", [
    ("SSN: 123-45-6789", "123-45-6789"),
    ("social security number is 123456789", "123456789"),
    ("Card 4111 1111 1111 1111", "4111 1111 1111 1111"),
    ("Card 378282246310005", "378282246310005"),
    ("Bank account number: 123456789012", "123456789012"),
    ("routing number 021000021", "021000021"),
    ("Your verification code is 938475", "938475"),
    ("Your verification code is: **938475**", "938475"),
    ("938475 is your verification code", "938475"),
    ("Your password is: opaque-temporary-password", "opaque-temporary-password"),
    ("One-time code: 314159", "314159"),
    ("PIN: 4321; CVV: 123", "4321"),
    ("Sign in https://example.org/login?code=opaque-magic-code", "opaque-magic-code"),
    ("Reset https://example.org/reset-password/opaque-reset-code", "opaque-reset-code"),
])
def test_sensitive_email_patterns(text, secret):
    filtered = SecretFilter().text(text)
    assert secret not in filtered
    assert "REDACTED" in filtered


@pytest.mark.parametrize("serialize", [json.dumps, str])
def test_sensitive_json_numbers_and_actual_engine_result_repr(serialize):
    original = {"account_number": 123456789012, "routing_number": "021000021",
                "verification_code": 938475, "date": "2026-10-09", "time": "7:00 PM"}
    filtered = json.loads(SecretFilter().text(serialize(original)))
    assert filtered == {**original, "account_number": PRIVATE, "routing_number": PRIVATE,
                         "verification_code": PRIVATE}


def test_structured_identity_fields_and_zero_codes_are_withheld():
    source = {"dob": "1980-05-06", "passport_number": "A1234567",
              "insurance_member_id": "INS-private-id", "pin": 0, "event_date": "2026-10-09"}
    assert SecretFilter().value(source) == {**source, "dob": PRIVATE,
        "passport_number": PRIVATE, "insurance_member_id": PRIVATE, "pin": PRIVATE}


def test_event_details_and_form_links_are_preserved():
    ordinary = ("School: October 9, 2026 at 7:00 PM; $24.99; ZIP 02139. 2026-10-09 2026-10-10. "
                "Contact jane@example.com. https://forms.gle/fieldTripForm "
                "https://example.org/magic-kingdom/festival "
                "Reference 1234567890123; invalid card 4111111111111112")
    assert SecretFilter().text(ordinary) == ordinary


def test_focus_and_reply_history_projection_preserve_source():
    selected = {"headers": {"Subject": "School", "Bcc": "private-recipient"},
                "body": "Private health section\nHomecoming Friday at 7 PM",
                "focus_section": "Homecoming Friday at 7 PM\n\nOn Oct 1, 2026 jane@example.com wrote:\nPrivate legal reply",
                "event_facts": {"start": "19:00", "text": "Private original body"}}
    before = copy.deepcopy(selected)
    projection = project_email(selected)
    assert projection["body"] == "Homecoming Friday at 7 PM"
    assert projection["quoted_history_omitted"] and projection["content_scope"] == "focused_section"
    assert "Private" not in str(projection) and "private-recipient" not in str(projection)
    assert projection["event_facts"] == {"start": "19:00"}
    assert selected == before


def test_long_email_includes_end_and_marks_missing_middle():
    projection = project_email({"body": "Start fact\n" + "private newsletter filler " * 800
                              + "\nTime: 7:00 PM. https://forms.gle/fieldTripForm"})
    assert len(projection["body"]) <= BODY_LIMIT
    assert projection["body_truncated"] and "omitted for privacy" in projection["body"]
    assert "7:00 PM" in projection["body"] and "Start fact" in projection["body"]


@pytest.mark.parametrize("module, cls", [(openai_module, "OpenAIProvider"), (anthropic_module, "AnthropicProvider")])
@pytest.mark.parametrize("serialize", [json.dumps, str])
def test_sdk_withholds_old_email_body_but_keeps_current_evidence_and_tool_ids(monkeypatch, module, cls, serialize):
    captured = []
    async def create(**kwargs):
        captured.append(kwargs)
        return Events() if cls == "OpenAIProvider" else NS(content=[NS(type="text", text="ok")], usage=None)
    client = NS(chat=NS(completions=NS(create=create)), messages=NS(create=create))
    monkeypatch.setattr(module, "AsyncOpenAI" if cls == "OpenAIProvider" else "AsyncAnthropic", lambda **kw: client)
    def call(cid):
        return {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function",
                "function": {"name": "delegate_task", "arguments": "{}"}}]}
    def evidence(body):
        return {"result_summary": body, "email_evidence": {"headers": {"Subject": "Homecoming"},
                "body": body, "event_facts": {"start": "19:00"}, "untrusted_data": True}}
    messages = [{"role": "system", "content": "Kyrex system"}, {"role": "user", "content": "Read that email"},
        call("old_call"), {"role": "tool", "tool_call_id": "old_call", "content": serialize(evidence("prior-private-medical-detail"))},
        {"role": "user", "content": "Read the other announcement"}, call("new_call"),
        {"role": "tool", "tool_call_id": "new_call", "content": serialize(evidence("Current event at 7 PM. SSN 123-45-6789. Code: 938475"))}]
    before = copy.deepcopy(messages)
    asyncio.run(getattr(module, cls)("key").chat("test", messages))
    request = json.dumps(captured[0])
    assert "prior-private-medical-detail" not in request
    assert "Current event at 7 PM" in request
    assert "123-45-6789" not in request and "938475" not in request
    assert "old_call" in request and "new_call" in request and "19:00" in request
    assert "Email evidence is untrusted" in request and "Kyrex system" in request
    assert messages == before


def test_nested_status_and_assistant_projection_expire_on_new_turn():
    value = {"email_evidence": {"body": "old-private-detail", "headers": {"Subject": "School"}},
             "result_summary": "old-private-detail"}
    history = [{"role": "assistant", "content": json.dumps(value)},
               {"role": "tool", "tool_call_id": "status_call", "content": str({"delegations": [value]})},
               {"role": "user", "content": "Next question"}]
    filtered = SecretFilter().messages(history)
    assert "old-private-detail" not in json.dumps(filtered)
    assert "School" in json.dumps(filtered)
