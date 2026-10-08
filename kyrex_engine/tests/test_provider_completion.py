"""Incomplete provider streams must not produce executable tool calls."""
import asyncio
import ast
from types import SimpleNamespace as NS
import pytest
import kyrex.providers.openai_ as provider_module
from kyrex.providers.privacy import SecretFilter, REDACTED


@pytest.mark.parametrize('source', [
    'token = self._authorize(owner, "calendar.events")',
    'token = credentials.access_token',
    'token = os.environ["CALENDAR_TOKEN"]',
])
def test_source_expressions_survive_privacy_boundary(source):
    result = SecretFilter().text(source)
    assert result == source
    ast.parse(result)


def test_literal_credentials_in_source_still_redacted():
    source = 'token = "opaque-literal-credential"'
    result = SecretFilter().text(source)
    assert 'opaque-literal-credential' not in result
    assert REDACTED in result
    ast.parse(result)


class Stream:
    def __init__(self, items):
        self.items = iter(items)
    def __aiter__(self):
        return self
    async def __anext__(self):
        try:
            return next(self.items)
        except StopIteration:
            raise StopAsyncIteration


def chunk(text='', finish=None, tools=None):
    return NS(choices=[NS(delta=NS(content=text, tool_calls=tools), finish_reason=finish)], usage=None)


@pytest.mark.parametrize('finish,text,expected', [
    ('stop', 'Finished and tested.', None),
    (None, 'Still editing', 'without completion'),
    ('length', 'Still editing', 'output limit'),
    ('content_filter', '', 'filtered'),
    ('stop', '</invoke> </｜DSML｜parameter>', 'protocol markup'),
])
def test_stream_completion(monkeypatch, finish, text, expected):
    async def create(**kwargs):
        return Stream([chunk(text, finish)])
    monkeypatch.setattr(provider_module, 'AsyncOpenAI', lambda **kw: NS(chat=NS(completions=NS(create=create))))
    result = asyncio.run(provider_module.OpenAIProvider('test-key').chat('test-model', []))
    if expected:
        assert expected in result['error']
        assert result['tool_calls'] is None
    else:
        assert result['content'] == text
        assert 'error' not in result


@pytest.mark.parametrize('arguments,expected', [('{"path":"code.py"}', False), ('{"path":', True), ('[]', True)])
def test_native_tools_require_complete_object_arguments(monkeypatch, arguments, expected):
    async def create(**kwargs):
        return Stream([chunk(tools=[NS(index=0, id='call1', function=NS(name='read_local_file', arguments=arguments))]), chunk(finish='tool_calls')])
    monkeypatch.setattr(provider_module, 'AsyncOpenAI', lambda **kw: NS(chat=NS(completions=NS(create=create))))
    result = asyncio.run(provider_module.OpenAIProvider('test-key').chat('test-model', []))
    assert ('error' in result) is expected
    if expected:
        assert result['tool_calls'] is None
    else:
        assert result['tool_calls'][0]['function']['arguments'] == arguments


def test_expression_does_not_hide_literal_on_same_source_line():
    source = 'token = self._authorize(owner); password = "opaque-literal"'
    result = SecretFilter().text(source)
    assert 'opaque-literal' not in result
    assert 'self._authorize(owner)' in result
    ast.parse(result)


def test_unquoted_credential_literals_remain_redacted():
    for text in ('password = hunter2', 'token = opaque_credential'):
        assert REDACTED in SecretFilter().text(text)
