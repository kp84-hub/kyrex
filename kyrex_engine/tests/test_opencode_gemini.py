"""Native Gemini HTTP requests and complete signed tool round trips."""
import asyncio
import copy
import json
import threading
from types import SimpleNamespace as NS

import httpx
import pytest

from kyrex.providers import get_provider
from kyrex.providers import opencode_gemini as gemini
from kyrex.providers.privacy import REDACTED

MODEL = 'gemini-4-argon-contributor'
BASE = 'https://opencode.ai/inference/go/openai/v1'
TOOLS = [{'type': 'function', 'function': {'name': 'read_local_file',
          'description': 'Read a file', 'parameters': {'type': 'object',
          'properties': {'path': {'type': 'string'}}, 'required': ['path']}}}]


def chunk(parts=(), finish=None, usage=None):
    result = {'candidates': [{'index': 0, 'content': {'role': 'model', 'parts': list(parts)}}]}
    if finish:
        result['candidates'][0]['finishReason'] = finish
    if usage:
        result['usageMetadata'] = usage
    return result


@pytest.fixture
def wire(monkeypatch):
    import kyrex.providers.openai_ as openai_module
    # The Gemini path uses native HTTP. Never initialize the unrelated SDK's
    # network/proxy transport in these otherwise-real HTTP payload tests.
    monkeypatch.setattr(openai_module, 'AsyncOpenAI', lambda **kwargs: NS())
    captured, replies = [], []
    def handle(request):
        captured.append(request)
        reply = replies.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in reply)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=raw)
    original = httpx.AsyncClient
    monkeypatch.setattr(gemini.httpx, 'AsyncClient',
                        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handle)))
    provider = get_provider('opencode', 'opaque-provider-credential', BASE,
                            extra_headers={'X-Tenant': 'opaque-tenant-credential'}, session_id='conv-a')
    return provider, captured, replies


@pytest.mark.parametrize('base', [BASE, 'https://opencode.ai/zen/go/v1',
                                'https://opencode.ai/inference/go/v1'])
def test_go_profiles_use_documented_google_endpoint(wire, base):
    provider, requests, replies = wire
    provider._client_kwargs['base_url'] = base
    replies.append([chunk([{'text': 'Hello'}, {'text': '', 'thoughtSignature': 'sig-text'}], 'STOP',
                           {'promptTokenCount': 11, 'candidatesTokenCount': 5, 'thoughtsTokenCount': 2})])
    emitted, final = [], []
    messages = [{'role': 'system', 'content': 'Be helpful'}, {'role': 'user', 'content': 'Hi'}]
    result = asyncio.run(provider.chat(MODEL, messages, tools=TOOLS,
                                      stream_callback=emitted.append, final_round_callback=final.append))
    request = requests[0]
    assert str(request.url) == 'https://opencode.ai/inference/go/v1/models/' + MODEL + ':streamGenerateContent?alt=sse'
    assert request.headers['authorization'] == 'Bearer opaque-provider-credential'
    assert request.headers['x-opencode-session'] == 'conv-a'
    assert request.headers['x-tenant'] == 'opaque-tenant-credential'
    assert request.headers['user-agent'].startswith('kyrex/')
    body = json.loads(request.content)
    assert body['systemInstruction'] == {'parts': [{'text': 'Be helpful'}]}
    assert body['contents'] == [{'role': 'user', 'parts': [{'text': 'Hi'}]}]
    assert body['tools'][0]['functionDeclarations'][0]['parametersJsonSchema'] == TOOLS[0]['function']['parameters']
    assert 'messages' not in body and 'model' not in body
    assert result['content'] == 'Hello' and emitted == ['Hello']
    assert result['usage'] == {'prompt_tokens': 11, 'completion_tokens': 7}
    assert result['_gemini']['parts'][-1]['thoughtSignature'] == 'sig-text'
    assert final == ['final_round_starting']


def test_parallel_tools_survive_engine_restart_and_return_signatures(wire, tmp_path):
    from kyrex.core import PlaneExecute
    from kyrex.session.tree import TreeSessionManager
    provider, requests, replies = wire
    native = [{'text': 'Checking'},
              {'functionCall': {'id': 'upstream-1', 'name': 'read_local_file', 'args': {'path': 'a.py'}},
               'thoughtSignature': 'sig-first'},
              {'functionCall': {'name': 'read_local_file', 'args': {'path': 'b.py'}}}]
    replies.append([chunk(native, 'STOP')])
    user = {'role': 'user', 'content': 'Read both files'}
    first = asyncio.run(provider.chat(MODEL, [user], tools=TOOLS))
    session = TreeSessionManager(str(tmp_path))
    session.append(user)
    session.append(first)
    for index, call in enumerate(first['tool_calls']):
        session.append({'role': 'tool', 'name': 'read_local_file', 'tool_call_id': call['id'],
                        'content': 'file-' + str(index)})
    session.save('main')
    restored = TreeSessionManager(str(tmp_path))
    restored.load('main')
    engine = object.__new__(PlaneExecute)
    engine.session = restored
    messages = engine._build_api_messages()
    assert messages[1]['_gemini'] == first['_gemini']
    replies.append([chunk([{'text': 'Both checked'}], 'STOP')])
    second = asyncio.run(provider.chat(MODEL, messages, tools=TOOLS))
    body = json.loads(requests[-1].content)
    assert body['contents'][1] == {'role': 'model', 'parts': native}
    assert body['contents'][2]['parts'] == [
        {'functionResponse': {'id': 'upstream-1', 'name': 'read_local_file', 'response': {'output': 'file-0'}}},
        {'functionResponse': {'name': 'read_local_file', 'response': {'output': 'file-1'}}}]
    assert second['content'] == 'Both checked'


def test_auth_never_enters_prompt_json_and_native_history_is_filtered(wire):
    provider, requests, replies = wire
    messages = [{'role': 'system', 'content': 'opaque-provider-credential'},
                {'role': 'assistant', 'content': 'opaque-tenant-credential', 'tool_calls': None,
                 '_gemini': {'model': MODEL, 'parts': [{'text': 'opaque-tenant-credential',
                                                     'thoughtSignature': 'signature-unchanged'}]}},
                {'role': 'user', 'content': 'Hi opaque-provider-credential'}]
    original = copy.deepcopy(messages)
    replies.append([chunk([{'text': 'OK'}], 'STOP')])
    result = asyncio.run(provider.chat(MODEL, messages))
    assert 'error' not in result
    body = json.loads(requests[0].content)
    assert 'opaque-provider-credential' not in requests[0].content.decode()
    assert 'opaque-tenant-credential' not in requests[0].content.decode()
    assert body['contents'][0]['parts'] == [{'text': REDACTED, 'thoughtSignature': 'signature-unchanged'}]
    assert messages == original


def test_withheld_assistant_text_is_not_restored_from_native_parts(wire):
    provider, requests, replies = wire
    messages = [{'role': 'assistant', 'content': 'Earlier evidence was withheld',
                 '_gemini': {'model': MODEL, 'parts': [{'text': 'private earlier email'},
                                                     {'text': '', 'thoughtSignature': 'sig'}]}},
                {'role': 'user', 'content': 'New question'}]
    replies.append([chunk([{'text': 'OK'}], 'STOP')])
    result = asyncio.run(provider.chat(MODEL, messages))
    assert 'error' not in result
    assert 'private earlier email' not in requests[0].content.decode()
    assert json.loads(requests[0].content)['contents'][0]['parts'][-1]['thoughtSignature'] == 'sig'


def test_thinking_and_text_deltas_stay_separate(wire):
    provider, requests, replies = wire
    replies.append([chunk([{'text': 'thinking', 'thought': True}]),
                    chunk([{'text': 'Hel'}]), chunk([{'text': 'lo'}], 'STOP')])
    text, reasoning = [], []
    result = asyncio.run(provider.chat(MODEL, [{'role': 'user', 'content': 'Hi'}],
                                      stream_callback=text.append, reasoning_callback=reasoning.append))
    assert result['content'] == 'Hello' and text == ['Hel', 'lo']
    assert result['reasoning_content'] == 'thinking' and reasoning == ['thinking']


@pytest.mark.parametrize('reply, expected', [
    ([chunk([{'functionCall': {'name': 'read_local_file', 'args': {}}}])], 'without completion'),
    ([chunk([{'functionCall': {'name': 'read_local_file', 'args': {}}}], 'MAX_TOKENS')], 'output limit'),
    ([chunk([], 'SAFETY')], 'filtered'),
    ([{'promptFeedback': {'blockReason': 'SAFETY'}}], 'filtered'),
    ([chunk([{'functionCall': {'name': 'read_local_file', 'args': []}}], 'STOP')], 'malformed tool calls'),
    ([chunk([{'text': '</invoke>'}], 'STOP')], 'protocol markup'),
    ([chunk([], 'STOP')], 'without completion'),
    ([chunk([{'text': 'partial'}]), {'error': {'message': 'private upstream error'}}], 'Provider request failed'),
])
def test_failed_stream_never_returns_executable_tools(wire, reply, expected):
    provider, requests, replies = wire
    replies.append(reply)
    result = asyncio.run(provider.chat(MODEL, [{'role': 'user', 'content': 'Hi'}], tools=TOOLS))
    assert expected in result['error'] and result['tool_calls'] is None
    assert 'private upstream error' not in str(result)
    assert len(requests) == 1


def test_cancellation_sends_nothing(wire):
    provider, requests, _ = wire
    cancel = threading.Event()
    cancel.set()
    result = asyncio.run(provider.chat(MODEL, [{'role': 'user', 'content': 'Hi'}], interrupt_event=cancel))
    assert 'interrupted' in result['error'] and not requests


def test_session_changes_apply_to_native_requests(wire):
    provider, requests, replies = wire
    provider.set_session_id('conv-b')
    replies.append([chunk([{'text': 'OK'}], 'STOP')])
    asyncio.run(provider.chat(MODEL, [{'role': 'user', 'content': 'Hi'}]))
    assert requests[0].headers['x-opencode-session'] == 'conv-b'


def test_other_provider_keeps_configured_chat_protocol(monkeypatch):
    import kyrex.providers.openai_ as openai_module
    captured = []
    async def create(**kwargs):
        captured.append(kwargs)
        async def stream():
            yield NS(choices=[NS(delta=NS(content='OK', tool_calls=None), finish_reason='stop')], usage=None)
        return stream()
    monkeypatch.setattr(openai_module, 'AsyncOpenAI', lambda **kw: NS(chat=NS(completions=NS(create=create))))
    provider = get_provider('openai', 'key', 'https://openrouter.ai/api/v1')
    message = {'role': 'assistant', 'content': None, '_gemini': {'model': MODEL, 'parts': []},
               'tool_calls': [{'id': 'call1', 'type': 'function', '_gemini_part': 0,
                               'function': {'name': 'read_local_file', 'arguments': '{}'}}]}
    result = asyncio.run(provider.chat(MODEL, [message]))
    assert result['content'] == 'OK' and '_gemini' not in json.dumps(captured[0])
    assert message['_gemini']['model'] == MODEL


def test_multiline_sse_and_final_event_without_blank_line():
    raw = 'data: {"candidates":\ndata: [], "usageMetadata": {"promptTokenCount": 3}}'
    async def collect():
        return [event async for event in gemini.events(httpx.Response(200, content=raw))]
    assert asyncio.run(collect()) == [{'candidates': [], 'usageMetadata': {'promptTokenCount': 3}}]


def test_http_error_stays_private(wire):
    provider, requests, replies = wire
    replies.append(httpx.Response(400, content='password=private-upstream-value'))
    result = asyncio.run(provider.chat(MODEL, [{'role': 'user', 'content': 'Hi'}]))
    assert 'HTTP 400' in result['error'] and 'private-upstream' not in str(result)


@pytest.mark.parametrize('base', ['https://opencode.ai/unknown/v1',
                                 'https://opencode.ai/zen/go/v1?key=private',
                                 'http://opencode.ai/zen/go/v1'])
def test_unknown_paths_fail_without_a_request(wire, base):
    provider, requests, _ = wire
    provider._client_kwargs['base_url'] = base
    result = asyncio.run(provider.chat(MODEL, [{'role': 'user', 'content': 'Hi'}]))
    assert 'error' in result and not requests
