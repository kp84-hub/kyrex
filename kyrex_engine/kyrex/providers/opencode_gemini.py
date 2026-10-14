"""Native Gemini wire protocol for OpenCode, using the existing Bot profile.

Go's documented Google SDK endpoint is /inference/go/v1/models/<model>.
Authentication and conversation headers stay separate from prompt JSON.
"""
from __future__ import annotations

import copy
import json
import re
import uuid
from urllib.parse import urlsplit, urlunsplit

import httpx


def requires_call_signature(model):
    # Current aliases inherit Gemini 3+ behavior; 1.x/2.x do not require it.
    return not re.match(r'gemini-(?:[12](?:[.-]|$)|pro(?:-vision)?$)', model, re.I)


SAFE_ERROR_CODES = frozenset({
    'inference_failed', 'INVALID_ARGUMENT', 'NOT_FOUND', 'PERMISSION_DENIED',
    'UNAUTHENTICATED', 'RESOURCE_EXHAUSTED', 'UNAVAILABLE',
    'OrgRequired', 'Forbidden', 'Unauthorized', 'NotFound', 'BadRequest',
    'InvalidRequest', 'InvalidArgument', 'UnsupportedModel', 'ModelNotFound',
    'ModelUnavailable', 'InferenceFailed', 'InferenceFailure', 'InferenceError',
    'UpstreamError', 'ProviderUnavailable', 'TooManyRequests', 'RateLimitExceeded',
    'BudgetExceeded', 'AccessDenied', 'InternalServerError',
})
REQUEST_ID_RE = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')


class GeminiHTTPError(RuntimeError):
    def __init__(self, status_code, code=None, request_id=None):
        self.status_code = status_code
        self.gemini_error_code = code if isinstance(code, str) and code in SAFE_ERROR_CODES else None
        self.request_id = request_id if isinstance(request_id, str) and REQUEST_ID_RE.fullmatch(request_id) else None
        super().__init__('Gemini request failed')


async def check_status(response, privacy=None):
    if not response.is_error:
        response.raise_for_status()
        return
    # Retain only allowlisted machine codes and UUID request IDs. Provider
    # messages/details, arbitrary tags, headers and prompts never leave here.
    raw = bytearray()
    for_header = [response.headers.get(name) for name in
                  ('x-request-id', 'x-opencode-request-id', 'request-id')]
    candidates = []
    async for block in response.aiter_bytes():
        if len(raw) + len(block) > 16384:
            break
        raw.extend(block)
    code = None
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            error = value.get('error')
            containers = [error, value] if isinstance(error, dict) else [value]
            candidates.extend(container.get(field) for container in containers
                              for field in ('request_id', 'requestId'))
            for container in containers:
                for field in ('_tag', 'type', 'code', 'status'):
                    candidate = container.get(field)
                    if isinstance(candidate, str) and candidate in SAFE_ERROR_CODES:
                        code = candidate
                        break
                if code:
                    break
    except (ValueError, AttributeError, TypeError):
        pass
    request_id = next((candidate for candidate in [*for_header, *candidates]
        if isinstance(candidate, str) and REQUEST_ID_RE.fullmatch(candidate)
        and (privacy is None or privacy.text(candidate) == candidate)), None)
    raise GeminiHTTPError(response.status_code, code, request_id)


def endpoint(base_url: str, model: str, *, stream=True) -> str:
    parsed = urlsplit(base_url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or not re.fullmatch(r'gemini-[A-Za-z0-9._-]+', model)):
        raise ValueError('Invalid Gemini gateway configuration')
    path = parsed.path.rstrip('/')
    if path in {'/zen/go/v1', '/inference/go/openai/v1', '/inference/go/v1'}:
        path = '/inference/go/v1'
    elif path == '/zen/v1':
        path = '/inference/v1'
    elif path == '/inference/openai/v1':
        path = '/inference/google/v1beta'
    elif not (path in {'/inference/v1', '/inference/google/v1beta',
                       '/inference/go/google/v1beta'}
              or re.fullmatch(r'/inference/custom/[A-Za-z0-9_-]+', path)):
        raise ValueError('Unsupported Gemini gateway endpoint')
    return urlunsplit((parsed.scheme, parsed.netloc,
                       path + '/models/' + model + (':streamGenerateContent' if stream else ':generateContent'),
                       'alt=sse' if stream else '', ''))


def text_parts(content):
    if isinstance(content, str):
        return [{'text': content}] if content else []
    parts = []
    for block in content or []:
        if block.get('type') == 'text':
            parts.append({'text': block.get('text', '')})
        elif block.get('type') == 'image_url':
            url = (block.get('image_url') or {}).get('url', '')
            match = re.fullmatch(r'data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)', url)
            if not match:
                raise ValueError('Gemini images must be inline data')
            parts.append({'inlineData': {'mimeType': match[1], 'data': match[2]}})
        else:
            raise ValueError('Unsupported Gemini content block')
    return parts


def payload(model, messages, tools, privacy):
    contents, instructions, calls = [], [], {}
    for message in messages:
        role = message.get('role')
        if role in {'system', 'developer'}:
            instructions.extend(text_parts(message.get('content')))
            continue
        if role == 'tool':
            previous = calls.get(message.get('tool_call_id'))
            if previous is None:
                raise ValueError('Orphaned Gemini tool response')
            name, native_id = previous
            response = {'name': name, 'response': {'output': message.get('content') or ''}}
            if native_id:
                response['id'] = native_id
            parts = [{'functionResponse': response}]
            # Parallel results belong in one user content, in received order.
            if contents and contents[-1]['role'] == 'user' and all(
                    'functionResponse' in part for part in contents[-1]['parts']):
                contents[-1]['parts'].extend(parts)
            else:
                contents.append({'role': 'user', 'parts': parts})
            continue
        if role not in {'user', 'assistant'}:
            raise ValueError('Unsupported Gemini message role')
        tool_calls = message.get('tool_calls') or []
        native = message.get('_gemini') or {}
        if role == 'assistant' and native.get('model') == model:
            # Retain the provider's part order, including signature-only chunks.
            # Text/arguments still pass the existing credential filter; opaque
            # thought signatures are returned byte-for-byte, never interpreted.
            parts = []
            active = {call.get('_gemini_part'): call for call in tool_calls}
            native_text = ''.join(part.get('text', '') for part in native.get('parts') or []
                                  if not part.get('thought'))
            canonical_text = message.get('content') or ''
            text_replaced = False
            for index, original in enumerate(native.get('parts') or []):
                part = copy.deepcopy(original)
                if 'functionCall' in part:
                    call = active.get(index)
                    if call is None:  # e.g. engine-consumed task_complete
                        continue
                    function = call['function']
                    part['functionCall']['name'] = function['name']
                    part['functionCall']['args'] = json.loads(function['arguments'])
                    calls[call['id']] = (function['name'], part['functionCall'].get('id'))
                if 'text' in part:
                    if not part.get('thought') and native_text != canonical_text:
                        # Withheld email evidence and compacted history must
                        # not be restored from an older protocol copy.
                        part['text'] = canonical_text if not text_replaced else ''
                        text_replaced = True
                    else:
                        part['text'] = privacy.text(part['text'])
                parts.append(part)
        else:
            parts = text_parts(message.get('content'))
            for index, call in enumerate(tool_calls):
                function = call['function']
                arguments = json.loads(function.get('arguments') or '{}')
                if not isinstance(arguments, dict):
                    raise RuntimeError('Provider returned malformed tool calls')
                part = {'functionCall': {'name': function['name'], 'args': arguments}}
                if index == 0 and requires_call_signature(model):
                    # Google's documented migration sentinel is only for calls
                    # reconstructed from another model or older saved history.
                    part['thoughtSignature'] = 'skip_thought_signature_validator'
                parts.append(part)
                calls[call['id']] = (function['name'], None)
        if parts:
            contents.append({'role': 'model' if role == 'assistant' else 'user', 'parts': parts})
    # Gemini's output default is model-specific. An unrequested, hardcoded
    # cap makes even a minimal connection check differ from the native API.
    body = {'contents': contents}
    if instructions:
        body['systemInstruction'] = {'parts': instructions}
    declarations = []
    for tool in tools or []:
        if tool.get('type') != 'function':
            raise ValueError('Unsupported Gemini tool')
        function = tool['function']
        declaration = {'name': function['name'], 'description': function.get('description', '')}
        if function.get('parameters'):
            # JSON Schema avoids dropping the engine's nested/union schemas.
            declaration['parametersJsonSchema'] = function['parameters']
        declarations.append(declaration)
    if declarations:
        body['tools'] = [{'functionDeclarations': declarations}]
    return body


async def events(response):
    """Decode SSE events, including multiline data and a final EOF event."""
    data = []
    size = 0
    async for line in response.aiter_lines():
        if line.startswith('data:'):
            data.append(line[5:].lstrip(' '))
            size += len(line)
            if size > 1024 * 1024:
                raise ValueError('Gemini stream event too large')
        elif not line and data:
            value = '\n'.join(data)
            if value != '[DONE]':
                yield json.loads(value)
            data, size = [], 0
    if data and '\n'.join(data) != '[DONE]':
        yield json.loads('\n'.join(data))


async def chat(provider, model, messages, tools, stream_callback,
               reasoning_callback, interrupt_event, final_round_callback):
    from .openai_ import validate_completion

    kwargs = provider._client_kwargs
    url = endpoint(str(kwargs.get('base_url') or ''), model)
    headers = dict(kwargs.get('default_headers') or {})
    # Preserve configured custom headers and the managed per-conversation ID.
    if not any(key.lower() == 'authorization' for key in headers):
        headers['Authorization'] = 'Bearer ' + str(kwargs.get('api_key') or '')
    if not any(key.lower() == 'user-agent' for key in headers):
        headers['User-Agent'] = 'kyrex/0.1'
    headers['Accept'] = 'text/event-stream'
    body = payload(model, messages, tools, provider._privacy)
    parts, text, reasoning, tool_calls = [], [], [], []
    finish, usage = None, None
    if interrupt_event is not None and interrupt_event.is_set():
        raise RuntimeError('Provider stream was interrupted')
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), follow_redirects=False) as client:
        async with client.stream('POST', url, headers=headers, json=body) as response:
            await check_status(response, provider._privacy)
            async for event in events(response):
                if interrupt_event is not None and interrupt_event.is_set():
                    raise RuntimeError('Provider stream was interrupted')
                if event.get('error'):
                    raise RuntimeError('Gemini provider stream failed')
                if (event.get('promptFeedback') or {}).get('blockReason'):
                    raise RuntimeError('Provider response was filtered')
                if event.get('usageMetadata') is not None:
                    usage = event['usageMetadata']
                candidates = event.get('candidates') or []
                if len(candidates) > 1:
                    raise ValueError('Unexpected Gemini candidate count')
                if not candidates:
                    continue
                candidate = candidates[0]
                if candidate.get('index', 0) != 0:
                    raise ValueError('Unexpected Gemini candidate index')
                for part in (candidate.get('content') or {}).get('parts') or []:
                    index = len(parts)
                    parts.append(part)
                    if 'text' in part:
                        delta = part['text']
                        if part.get('thought'):
                            reasoning.append(delta)
                            if delta and reasoning_callback:
                                reasoning_callback(delta)
                        else:
                            text.append(delta)
                            if delta and stream_callback:
                                stream_callback(delta)
                    elif 'functionCall' in part:
                        function = part['functionCall']
                        args = function.get('args', {})
                        if not isinstance(args, dict) or not function.get('name'):
                            raise RuntimeError('Provider returned malformed tool calls')
                        # Provider IDs are optional. Local IDs bind each parallel
                        # result to its native part, including repeated names.
                        tool_calls.append({'id': 'gemini_' + uuid.uuid4().hex,
                                           'type': 'function', '_gemini_part': index,
                                           'function': {'name': function['name'],
                                                        'arguments': json.dumps(args)}})
                    elif set(part) != {'thoughtSignature'}:
                        raise ValueError('Unsupported Gemini response part')
                if candidate.get('finishReason'):
                    finish = candidate['finishReason']
    if finish == 'MAX_TOKENS':
        raise RuntimeError('Provider response reached its output limit')
    if finish in {'SAFETY', 'RECITATION', 'BLOCKLIST', 'PROHIBITED_CONTENT', 'SPII', 'IMAGE_SAFETY'}:
        raise RuntimeError('Provider response was filtered')
    if finish != 'STOP':
        raise RuntimeError('Provider stream ended without completion')
    first_call = next((part for part in parts if 'functionCall' in part), None)
    if (first_call is not None and requires_call_signature(model)
            and (not isinstance(first_call.get('thoughtSignature'), str)
                 or not first_call['thoughtSignature'])):
        raise RuntimeError('Gemini returned a tool call without its required thought signature')
    content = ''.join(text)
    validate_completion(content, tool_calls)
    if not content and not tool_calls:
        raise RuntimeError('Provider stream ended without completion')
    if final_round_callback and content and not tool_calls:
        final_round_callback('final_round_starting')
    result = {'role': 'assistant', 'content': content or None,
              'tool_calls': tool_calls or None, '_gemini': {'model': model, 'parts': parts}}
    if reasoning:
        result['reasoning_content'] = ''.join(reasoning)
    if usage:
        result['usage'] = {'prompt_tokens': usage.get('promptTokenCount', 0),
                           'completion_tokens': usage.get('candidatesTokenCount', 0) + usage.get('thoughtsTokenCount', 0)}
    return result
