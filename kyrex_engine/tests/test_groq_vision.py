"""Validate the actual Groq request adapter with synthetic image input."""
import asyncio
from types import SimpleNamespace as NS

import pytest
import kyrex.providers.openai_ as module


@pytest.mark.parametrize('base,expected', [
    ('https://api.groq.com/openai/v1', 16384),
    ('https://openrouter.ai/api/v1', 32768),
])
def test_completion_limit_and_image_bytes(monkeypatch, base, expected):
    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        async def chunks():
            yield NS(choices=[NS(delta=NS(content='Blue robot.', tool_calls=None), finish_reason='stop')], usage=None)
        return chunks()
    monkeypatch.setattr(module, 'AsyncOpenAI', lambda **kwargs: NS(chat=NS(completions=NS(create=create))))
    image = {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,cGl4ZWxz'}}
    result = asyncio.run(module.OpenAIProvider('synthetic-provider-key', base_url=base).chat(
        'qwen/qwen3.8-27b', [{'role': 'user', 'content': [{'type': 'text', 'text': 'Describe this'}, image]}]))
    assert result['content'] == 'Blue robot.'
    assert calls[0]['max_tokens'] == expected
    assert calls[0]['messages'][0]['content'][1] == image
    assert 'tools' not in calls[0]
