"""Explicit OpenCode checks using an owner's saved Bot provider selection."""
import asyncio
import json
import time
import uuid
from urllib.parse import urlsplit

import httpx

import bot_provider
import chat_onboarding
from kyrex.providers import get_provider
from kyrex.providers.privacy import safe_provider_error


async def workspace_check(cfg, session_id):
    # Only the documented first-party Console endpoint. Never relay arbitrary
    # profile headers or an upstream config document back to the browser.
    headers = {key: value for key, value in cfg['headers'].items()
               if key.lower() in {'authorization', 'x-opencode-org-id', 'x-org-id'}}
    if not any(key.lower() == 'authorization' for key in headers):
        headers['Authorization'] = 'Bearer ' + cfg['api_key']
    headers.update({'User-Agent': 'kyrex/0.1', 'x-opencode-session': session_id})
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5), follow_redirects=False) as client:
            async with client.stream('GET', 'https://opencode.ai/console/api/v2/config', headers=headers) as response:
                if response.status_code != 200:
                    return {'available': False, 'http_status': response.status_code}
                raw = bytearray()
                async for block in response.aiter_bytes():
                    if len(raw) + len(block) > 1024 * 1024:
                        return {'available': False}
                    raw.extend(block)
        value = json.loads(raw)
        providers = value.get('providers')
        if not isinstance(providers, dict):
            return {'available': False}
        listed = any(isinstance(provider, dict) and isinstance(provider.get('models'), dict)
                     and cfg['model'] in provider['models'] for provider in providers.values())
        return {'available': True, 'model_listed': listed}
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return {'available': False}


def reserve_check(user):
    # Share the first-run test allowance: multiple Bots cannot bypass it.
    with chat_onboarding.database() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM checks WHERE at<?', (time.time() - 60,))
        if db.execute('SELECT COUNT(*) FROM checks WHERE owner=?', (user,)).fetchone()[0] >= 5:
            raise chat_onboarding.SetupError('Wait a minute before another model check.')
        db.execute('INSERT INTO checks VALUES (?,?)', (user, time.time()))


async def check(user, bot, selection):
    cfg = bot_provider.resolve_bot_provider(user, {**bot, **selection})
    parsed = urlsplit(cfg['base_url'])
    if parsed.scheme != 'https' or parsed.hostname != 'opencode.ai' or parsed.username or parsed.password:
        raise chat_onboarding.SetupError('Choose an OpenCode provider profile for this check.')
    if cfg['provider'] == 'anthropic':
        raise chat_onboarding.SetupError('Choose an OpenCode or OpenAI-compatible profile for this model check.')
    await asyncio.to_thread(reserve_check, user)
    session_id = 'kyrex-model-check-' + uuid.uuid4().hex
    workspace = await workspace_check(cfg, session_id)
    provider = None
    result = {'model': cfg['model'], 'workspace': workspace}
    try:
        model_headers = {key: value for key, value in cfg['headers'].items() if key.lower() != 'user-agent'}
        model_headers['User-Agent'] = 'kyrex/0.1'
        provider = get_provider(cfg['provider'], cfg['api_key'], cfg['base_url'],
                                extra_headers=model_headers, session_id=session_id)
        reply = await asyncio.wait_for(provider.chat(
            model=cfg['model'], messages=[{'role': 'user', 'content': 'Reply briefly with OK.'}],
            tools=[]), timeout=35)
        if reply.get('error'):
            # OpenCode goes through the existing privacy-filtered adapter.
            result.update(ok=False, message=reply['error'])
        elif reply.get('tool_calls') or not isinstance(reply.get('content'), str) or not reply['content'].strip():
            result.update(ok=False, message='OpenCode did not return a usable test reply.')
        else:
            result.update(ok=True, message='OpenCode accepted this model and returned a test reply.')
    except Exception as exc:
        result.update(ok=False, message=safe_provider_error(exc))
    finally:
        if provider is not None:
            try:
                await provider._client.close()
            except Exception:
                pass
    try:
        current = bot_provider.resolve_bot_provider(user, {**bot, **selection})
    except bot_provider.BotProviderError:
        current = None
    if current != cfg:
        result.update(ok=False, workspace={'available': False},
                      message='Your provider profile changed during the check. Check the current selection again.')
    return result
