"""Anthropic Messages wire adapter for the shared OpenCode Go profile."""
import json
import re

from anthropic import AsyncAnthropic


def blocks(content):
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content else []
    result = []
    for block in content or []:
        if block.get('type') == 'text':
            result.append({'type': 'text', 'text': block.get('text', '')})
        elif block.get('type') == 'image_url':
            url = (block.get('image_url') or {}).get('url', '')
            match = re.fullmatch(r'data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)', url)
            if not match:
                raise ValueError('Go images must be inline data')
            result.append({'type': 'image', 'source': {'type': 'base64',
                          'media_type': match[1], 'data': match[2]}})
        else:
            raise ValueError('Unsupported Go content block')
    return result


def payload(model, messages, tools):
    system, history = [], []
    for message in messages:
        role = message.get('role')
        if role in {'system', 'developer'}:
            system.extend(blocks(message.get('content')))
            continue
        if role == 'tool':
            role = 'user'
            content = [{'type': 'tool_result', 'tool_use_id': message['tool_call_id'],
                        'content': message.get('content') or ''}]
        elif role in {'user', 'assistant'}:
            content = blocks(message.get('content'))
            for call in message.get('tool_calls') or []:
                arguments = json.loads(call['function']['arguments'])
                if not isinstance(arguments, dict):
                    raise ValueError('Malformed Go tool arguments')
                content.append({'type': 'tool_use', 'id': call['id'],
                    'name': call['function']['name'], 'input': arguments})
        else:
            raise ValueError('Unsupported Go message role')
        # Imported reasoning/signatures belong to their original model. Go
        # thinking is not enabled here; only canonical text and tools migrate.
        if not content:
            continue
        if history and history[-1]['role'] == role:
            history[-1]['content'].extend(content)
        else:
            history.append({'role': role, 'content': content})
    value = {'model': model, 'messages': history, 'max_tokens': 8192}
    if system:
        value['system'] = system
    if tools:
        value['tools'] = [{'name': t['function']['name'],
            'description': t['function'].get('description', ''),
            'input_schema': t['function']['parameters']}
            for t in tools if t.get('type') == 'function']
    return value


async def chat(owner, model, messages, tools, stream_callback, reasoning_callback,
               interrupt_event, final_round_callback):
    from .openai_ import validate_completion
    headers = dict(owner._client_kwargs.get('default_headers') or {})
    # The Go service account key is a Bearer credential on every protocol.
    headers['Authorization'] = 'Bearer ' + owner._client_kwargs['api_key']
    async with AsyncAnthropic(api_key=owner._client_kwargs['api_key'],
            base_url=owner._client_kwargs['base_url'].removesuffix('/v1'), default_headers=headers,
            max_retries=0, timeout=120) as client:
        text = []
        completed = False
        async with client.messages.stream(**payload(model, messages, tools)) as stream:
            async for event in stream:
                if interrupt_event is not None and interrupt_event.is_set():
                    raise RuntimeError('Go Messages request interrupted')
                if event.type == 'message_stop':
                    completed = True
                if event.type == 'content_block_delta' and event.delta.type == 'text_delta':
                    text.append(event.delta.text)
                    if stream_callback:
                        stream_callback(event.delta.text)
                elif event.type == 'content_block_delta' and event.delta.type == 'thinking_delta':
                    if reasoning_callback:
                        reasoning_callback(event.delta.thinking)
            if not completed:
                raise RuntimeError('Go Messages stream ended without completion')
            response = await stream.get_final_message()
    if response.stop_reason not in {'end_turn', 'tool_use', 'stop_sequence'}:
        raise RuntimeError('Go Messages response incomplete')
    calls = []
    for block in response.content:
        if block.type == 'tool_use':
            calls.append({'id': block.id, 'type': 'function', 'function': {
                'name': block.name, 'arguments': json.dumps(block.input)}})
    content = ''.join(text) or ''.join(b.text for b in response.content if b.type == 'text')
    validate_completion(content, calls)
    if not content and not calls:
        raise RuntimeError('Go Messages returned an empty response')
    if response.stop_reason == 'tool_use' and not calls:
        raise RuntimeError('Go Messages response missing tool calls')
    if content and not text and stream_callback:
        stream_callback(content)
    if final_round_callback and content and not calls:
        final_round_callback('final_round_starting')
    return {'role': 'assistant', 'content': content or None, 'tool_calls': calls or None,
            'usage': {'prompt_tokens': response.usage.input_tokens,
                      'completion_tokens': response.usage.output_tokens}}
