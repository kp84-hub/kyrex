"""Actual SDK wire requests: one saved Go base/key, every Go protocol."""
import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI
from anthropic import AsyncAnthropic
import anthropic._base_client as anthropic_base

import kyrex.providers.openai_ as openai_module
import kyrex.providers.opencode_messages as messages_module
from kyrex.opencode_go import GO_BASE, GO_MODELS, go_base


def sse(*values):
    return ''.join(('event: ' + value['type'] + '\n' if 'type' in value else '') + 'data: ' + json.dumps(value) + '\n\n' for value in values)


@pytest.fixture
def wire(monkeypatch):
    class Requests(list):
        override = None
    requests = Requests()
    def handle(request):
        value = json.loads(request.content)
        requests.append((request, value))
        path = request.url.path
        if requests.override is not None:
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=requests.override)
        if path.endswith('/chat/completions'):
            data = sse({'id': 'test', 'object': 'chat.completion.chunk', 'created': 0,
                'model': value['model'], 'choices': [{'index': 0, 'delta': {'content': 'OK'}, 'finish_reason': 'stop'}]})
        elif path.endswith('/responses'):
            data = sse({'type': 'response.completed', 'response': {'id': 'test',
                'object': 'response', 'created_at': 0, 'model': value['model'], 'status': 'completed',
                'output': [{'type': 'message', 'id': 'msg', 'role': 'assistant', 'status': 'completed',
                            'content': [{'type': 'output_text', 'text': 'OK', 'annotations': []}]}]}})
        elif path.endswith('/messages'):
            data = sse({'type': 'message_start', 'message': {'id': 'test', 'type': 'message',
                'role': 'assistant', 'model': value['model'], 'content': [], 'stop_reason': None,
                'stop_sequence': None, 'usage': {'input_tokens': 5, 'output_tokens': 0}}},
                {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'OK'}},
                {'type': 'content_block_stop', 'index': 0},
                {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 1}},
                {'type': 'message_stop'})
        elif ':streamGenerateContent' in path:
            data = sse({'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'OK'}]}, 'finishReason': 'STOP'}]})
        else:
            raise AssertionError(str(request.url))
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=data)
    transport = httpx.MockTransport(handle)
    original_http = httpx.AsyncClient
    class http_client(original_http):
        def __init__(self, **kwargs):
            super().__init__(**{**kwargs, 'transport': transport, 'trust_env': False})
    monkeypatch.setattr(httpx, 'AsyncClient', http_client)
    monkeypatch.setattr(openai_module, 'AsyncOpenAI', lambda **kwargs: AsyncOpenAI(**kwargs, http_client=http_client()))
    native_http = getattr(anthropic_base, "httpx2", httpx)
    def native_handler(request):
        reply = handle(request)
        return native_http.Response(reply.status_code, headers=reply.headers, content=reply.content)
    monkeypatch.setattr(messages_module, 'AsyncAnthropic', lambda **kwargs: AsyncAnthropic(**kwargs,
        http_client=native_http.AsyncClient(transport=native_http.MockTransport(native_handler), trust_env=False)))
    return requests


# Independently recorded from the official endpoint table, not the router.
ROUTES = {
    '/responses': ['gpt-6-luna', 'gpt-5.6-luna', 'grok-4.7', 'grok-4.6', 'muse-spark-1.3-contributor', 'muse-spark-1.2-contributor'],
    '/messages': ['claude-haiku-5-5', 'minimax-m3', 'minimax-m2.7', 'qwen3.8-max', 'qwen3.8-flash', 'qwen3.7-plus'],
    '/chat/completions': ['deepseek-v4.1-flash', 'deepseek-v4-pro', 'glm-5.3-flash', 'glm-5.3', 'glm-5.2',
        'kimi-k3', 'kimi-k2.7-code', 'kimi-k2.6', 'longcat-2.0', 'mimo-v2.6-flash', 'mimo-v2.6-pro',
        'mimo-v2.5', 'mimo-v2.5-pro', 'hy4-preview', 'hy3', 'space-bunny', 'longcat-2.5-preview-free', 'step-5-preview-free', 'cat-jam-free'],
    '/models/gemini-4-argon-contributor:streamGenerateContent': ['gemini-4-argon-contributor'],
}


@pytest.mark.parametrize('suffix,model', [(path, model) for path, models in ROUTES.items() for model in models])
def test_every_documented_go_model_uses_one_base_key_and_session(wire, suffix, model):
    async def run():
        provider = openai_module.OpenAIProvider('test-saved-key', 'https://opencode.ai/zen/go/v1', session_id='conversation-1')
        result = await provider.chat(model, [{'role': 'user', 'content': 'Hello'}])
        await provider._client.close()
        return result
    result = asyncio.run(run())
    assert result.get('content') == 'OK', result
    request, value = wire[-1]
    assert str(request.url).split('?')[0] == GO_BASE + suffix
    assert request.headers['authorization'] == 'Bearer test-saved-key'
    assert request.headers['x-opencode-session'] == 'conversation-1'
    assert request.headers['user-agent'] == 'kyrex/0.1'
    assert len(wire) == 1


def test_same_provider_switches_across_all_protocols_with_tool_history(wire):
    history = [{'role': 'system', 'content': 'System instruction'},
        {'role': 'developer', 'content': 'Developer instruction'},
        {'role': 'user', 'content': 'Read files'},
        {'role': 'assistant', 'content': None, 'reasoning_content': 'old unsigned reasoning',
         '_gemini': {'model': 'gemini-4-argon-contributor', 'parts': [{'thoughtSignature': 'old-signature'}]},
         'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}},
                        {'id': 'call_2', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}}]},
        {'role': 'tool', 'name': 'read', 'tool_call_id': 'call_1', 'content': 'first'},
        {'role': 'tool', 'name': 'read', 'tool_call_id': 'call_2', 'content': 'second'},
        {'role': 'user', 'content': 'Continue'}]
    tools = [{'type': 'function', 'function': {'name': 'read', 'parameters': {'type': 'object', 'properties': {}}}}]
    async def run():
        provider = openai_module.OpenAIProvider('test-saved-key', GO_BASE, session_id='same-conversation')
        for model in ['deepseek-v4.1-flash', 'gpt-6-luna', 'claude-haiku-5-5']:
            result = await provider.chat(model, history, tools=tools)
            assert result.get('content') == 'OK', result
        await provider._client.close()
    asyncio.run(run())
    assert len(wire) == 3
    assert {r.headers['x-opencode-session'] for r, _ in wire} == {'same-conversation'}
    native = wire[-1][1]
    assert [r['type'] for r in native['messages'][2]['content'][:2]] == ['tool_result', 'tool_result']
    assert [r['tool_use_id'] for r in native['messages'][2]['content'][:2]] == ['call_1', 'call_2']
    assert [r['text'] for r in native['system']] == ['System instruction', 'Developer instruction']
    assert 'old unsigned reasoning' not in json.dumps(native) and 'thoughtSignature' not in json.dumps(native)
    assert native['tools'][0]['input_schema'] == tools[0]['function']['parameters']
    assert history[3]['_gemini']['parts'][0]['thoughtSignature'] == 'old-signature'


@pytest.mark.parametrize('base', ['https://opencode.ai/zen/go/v1/responses', 'https://opencode.ai/inference/go/openai/v1', GO_BASE])
def test_endpoint_profiles_and_cli_model_prefix_are_normalized(wire, base):
    async def run():
        provider = openai_module.OpenAIProvider('test-saved-key', base, session_id='s')
        result = await provider.chat('opencode-go/gpt-6-luna', [{'role': 'user', 'content': 'Hello'}])
        await provider._client.close()
        return result
    assert asyncio.run(run())['content'] == 'OK'
    assert wire[0][1]['model'] == 'gpt-6-luna'
    assert str(wire[0][0].url) == GO_BASE + '/responses'


def test_catalog_has_all_documented_models():
    assert set(GO_MODELS) == {model for models in ROUTES.values() for model in models}


@pytest.mark.parametrize('base', ['https://openrouter.ai/api/v1', 'https://opencode.ai/inference/openai/v1', 'http://opencode.ai/zen/go/v1', 'https://opencode.ai.evil.test/inference/go/v1'])
def test_go_normalization_is_scoped(base):
    assert go_base(base) is None


@pytest.mark.parametrize('ending', ['tool_use', 'max_tokens', 'missing_stop', 'malformed_arguments'])
def test_messages_returns_tools_only_after_valid_complete_stream(wire, ending):
    content = {'type': 'tool_use', 'id': 'call_new', 'name': 'read', 'input': {}}
    events = [{'type': 'message_start', 'message': {'id': 'test', 'type': 'message',
        'role': 'assistant', 'model': 'claude-haiku-5-5', 'content': [], 'stop_reason': None,
        'stop_sequence': None, 'usage': {'input_tokens': 5, 'output_tokens': 0}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': content},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta',
         'partial_json': '[1]' if ending == 'malformed_arguments' else '{"path":"."}'}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens' if ending == 'max_tokens' else 'tool_use',
                                          'stop_sequence': None}, 'usage': {'output_tokens': 1}}]
    if ending != 'missing_stop':
        events.append({'type': 'message_stop'})
    wire.override = sse(*events)
    async def run():
        provider = openai_module.OpenAIProvider('test-saved-key', GO_BASE, session_id='s')
        reply = await provider.chat('claude-haiku-5-5', [{'role': 'user', 'content': 'Read'}])
        await provider._client.close()
        return reply
    reply = asyncio.run(run())
    if ending == 'tool_use':
        assert reply['tool_calls'][0]['id'] == 'call_new'
        assert json.loads(reply['tool_calls'][0]['function']['arguments']) == {'path': '.'}
        assert reply['usage'] == {'prompt_tokens': 5, 'completion_tokens': 1}
    else:
        assert reply.get('error') and not reply.get('tool_calls')
    assert len(wire) == 1, 'Invalid completion never triggers a protocol fallback or replay'
