"""Deterministic first-run setup. Never requires a model to configure a model."""
import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

import requests
import bot_provider
import chat_service
import provider_profiles
import serve
from paths import data_dir

PROVIDERS = {
    'opencode': {'label': 'OpenCode Go', 'base_url': 'https://opencode.ai/zen/go/v1', 'provider': 'opencode'},
    'openrouter': {'label': 'OpenRouter', 'base_url': 'https://openrouter.ai/api/v1', 'provider': 'openrouter'},
    'openai': {'label': 'OpenAI', 'base_url': 'https://api.openai.com/v1', 'provider': 'openai'},
    'anthropic': {'label': 'Anthropic', 'base_url': 'https://api.anthropic.com', 'provider': 'anthropic'},
}
WELCOME = "Welcome to Kyrex. I'm The Overwatcher. What would you like help with first—your calendar and email, development projects, fitness, or something else?"
PROMPT = """You are The Overwatcher, this user's Kyrex Chat assistant. Guide new users
through useful setup with a friendly conversation. Ask one question at a time,
keep messages short, and adapt to their answers. Their chat provider is already
connected; build on verified account settings and connection status.
Learn their preferred name and main goal. Confirm their timezone when scheduling
matters. Guide them through only the connections and Bots needed for their goal.
Use only this owner's account data and preferences. Never claim a connection or
action succeeded without a verified result. Direct credential entry to Settings
or Connections; never ask for keys, passwords, or service account JSON in Chat.
Explain separate provider billing before optional voice setup; let the user pick
speech providers and preview the saved voice. Let users skip optional steps.
Help them complete one small task they choose. Once initial setup is done, work
as their ordinary assistant; do not repeat onboarding in each conversation.
"""


class SetupError(ValueError):
    pass


@contextmanager
def database():
    path = data_dir() / 'chat_onboarding.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.execute('CREATE TABLE IF NOT EXISTS setup (owner TEXT PRIMARY KEY, value TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS checks (owner TEXT, at REAL)')
    db.commit(); path.chmod(0o600)
    try:
        with db: yield db
    finally:
        db.close()


def read(db, user):
    row = db.execute('SELECT value FROM setup WHERE owner=?', (user,)).fetchone()
    return json.loads(row[0]) if row else {}


def write(db, user, state):
    db.execute('INSERT OR REPLACE INTO setup VALUES (?,?)', (user, json.dumps(state)))


def profile_for(user, state):
    profile = provider_profiles.get_profile(user, state.get('profile_id', ''))
    if not profile or not profile.get('api_key'):
        raise SetupError('Save your provider credentials first.')
    return profile


def fingerprint(profile, model):
    secret = os.environ.get('WEB_SESSION_SECRET') or os.environ.get('KYREX_PROVIDER_SECRETS_KEY')
    if not secret:
        raise SetupError('Account credential encryption is unavailable. Contact the server operator.')
    return hmac.new(secret.encode(), json.dumps([profile, model], sort_keys=True).encode(), hashlib.sha256).hexdigest()


def verified(profile, state):
    return bool(state.get('model') and time.time() - state.get('verified_at', 0) < 900
                and hmac.compare_digest(state.get('verified', ''), fingerprint(profile, state['model'])))


def existing_overwatcher(user):
    for bot in sorted(chat_service.bots.load_bots().values(), key=lambda b: b['id']):
        if bot.get('owner') != user or not serve.coordinator_granted(bot):
            continue
        try:
            bot_provider.resolve_bot_provider(user, bot)
            return bot
        except bot_provider.BotProviderError:
            pass
    return None


def status(user):
    with database() as db:
        state = read(db, user)
    profile = provider_profiles.get_profile(user, state.get('profile_id', ''))
    bot = existing_overwatcher(user)
    tested = bool(profile and verified(profile, state))
    return {'required': bot is None, 'step': 'ready' if bot else 'finish' if tested else 'model' if profile else 'provider',
            'profile_id': profile['id'] if profile else None, 'model': state.get('model', ''),
            'verified': tested, 'bot_id': bot['id'] if bot else None,
            'conversation_id': state.get('conversation_id') if bot and bot['id'] == state.get('bot_id') else None,
            'providers': [{'id': k, **v} for k, v in PROVIDERS.items()],
            'profiles': provider_profiles.list_profiles(user)}


def configure(user, data):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        state = read(db, user)
        selection = data.get('profile_id')
        if selection is not None:
            if not isinstance(selection, str) or not provider_profiles.get_profile(user, selection):
                raise SetupError('Choose one of your saved provider profiles.')
            profile_id = selection
        else:
            choice = data.get('provider')
            if not isinstance(choice, str) or choice not in PROVIDERS:
                raise SetupError('Choose a supported provider.')
            key = data.get('api_key')
            if not isinstance(key, str) or not 8 <= len(key.strip()) <= 512 or not key.strip().isascii() or any(c.isspace() for c in key.strip()):
                raise SetupError('Enter your provider API key in the setup form.')
            # Wizard-managed profiles never overwrite existing profiles chosen by the user.
            profile_id = state.get('draft_profile_id') or 'setup-' + uuid.uuid4().hex[:16]
            provider_profiles.save_profile(user, {'id': profile_id, 'name': PROVIDERS[choice]['label'],
                'provider': PROVIDERS[choice]['provider'], 'base_url': PROVIDERS[choice]['base_url'],
                'api_key': key.strip(), 'models': []}, allow_empty_models=True)
            state['draft_profile_id'] = profile_id
        state.update(profile_id=profile_id, model='', verified='', verified_at=0)
        write(db, user, state)
    return status(user)


def response_json(response, limit=65536):
    try:
        raw = bytearray()
        for chunk in response.iter_content(8192):
            raw.extend(chunk)
            if len(raw) > limit:
                raise SetupError('The provider response was too large.')
        if response.status_code != 200:
            code = response.status_code
            if code in (401, 403): raise SetupError('The provider rejected your credentials or model access. Check them in setup.')
            if code in (402, 429): raise SetupError('The provider reported a billing, quota or rate limit. Check your provider account before testing again.')
            if code in (400, 404): raise SetupError('The provider could not use this model. Check the exact model ID.')
            raise SetupError('The provider is unavailable. No automatic retry was made.')
        value = json.loads(raw)
        if not isinstance(value, dict): raise ValueError()
        return value
    except (ValueError, TypeError) as exc:
        if isinstance(exc, SetupError): raise
        raise SetupError('The provider returned an unreadable response.') from None
    finally:
        response.close()


def request_profile(profile, path, *, data=None, gemini_model=None):
    # New setup uses only first-party preset destinations. Custom saved profiles
    # remain usable through the existing Settings/Bots advanced flow.
    base = profile['base_url'].rstrip('/')
    if base not in {v['base_url'] for v in PROVIDERS.values()}:
        raise SetupError('This setup wizard supports the listed provider endpoints. Use Settings and Bots for a custom endpoint.')
    url = base + path
    if gemini_model is not None:
        from kyrex.providers.opencode_gemini import endpoint
        if profile['provider'] != 'opencode':
            raise SetupError('Native Gemini setup requires an OpenCode profile.')
        try:
            url = endpoint(base, gemini_model, stream=False)
        except ValueError:
            raise SetupError('Check the exact Gemini model ID.') from None
    headers = dict(profile.get('headers') or {})
    headers['User-Agent'] = 'Kyrex-Chat-Setup/1.0'
    if profile['provider'] == 'anthropic':
        headers.update({'x-api-key': profile['api_key'], 'anthropic-version': '2023-06-01'})
    else:
        headers['Authorization'] = 'Bearer ' + profile['api_key']
    if profile['provider'] == 'opencode':
        headers['x-opencode-session'] = 'kyrex-setup-' + profile['id']
    try:
        response = requests.request('POST' if data is not None else 'GET', url,
            headers=headers, json=data, timeout=(5, 20), allow_redirects=False, stream=True)
        return response_json(response, 4 * 1024 * 1024 if path.endswith('/models') else 65536)
    except requests.RequestException:
        raise SetupError('The provider connection failed. No automatic retry was made.') from None


def models(user):
    with database() as db: state = read(db, user)
    profile = profile_for(user, state)
    try:
        value = request_profile(profile, '/v1/models' if profile['provider'] == 'anthropic' else '/models')
        ids = [item['id'] for item in value.get('data', []) if isinstance(item, dict)
               and isinstance(item.get('id'), str) and 0 < len(item['id']) <= 200 and item['id'].isprintable()]
        return {'models': sorted(set(ids + profile.get('models', [])))[:1000]}
    except (SetupError, AttributeError, TypeError):
        return {'models': profile.get('models', []), 'notice': 'Model discovery is unavailable. Enter the exact model ID from your provider.'}


def probe(profile, model):
    message = {'role': 'user', 'content': 'Reply briefly with OK to test this coding assistant connection.'}
    if profile['provider'] == 'anthropic':
        value = request_profile(profile, '/v1/messages', data={'model': model, 'messages': [message], 'max_tokens': 64})
        valid = isinstance(value.get('content'), list) and bool(value['content'])
    elif profile['provider'] == 'opencode' and model.startswith('gemini-'):
        # A small independent native request distinguishes account/model access
        # from failures involving a real conversation's tools or saved history.
        value = request_profile(profile, '', gemini_model=model, data={
            'contents': [{'role': 'user', 'parts': [{'text': message['content']}]}]})
        candidates = value.get('candidates')
        candidate = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
        content = candidate.get('content')
        parts = content.get('parts') if isinstance(content, dict) else None
        valid = bool(candidate.get('finishReason') == 'STOP' and isinstance(parts, list)
                     and any(isinstance(part, dict) and isinstance(part.get('text'), str)
                             and part['text'].strip() and not part.get('thought') for part in parts))
    else:
        from kyrex.providers.openai_ import _OPENCODE_RESPONSES_MODELS
        if profile['provider'] == 'opencode' and model in _OPENCODE_RESPONSES_MODELS:
            value = request_profile(profile, '/responses', data={'model': model, 'input': [message], 'max_output_tokens': 64, 'store': False})
            valid = value.get('object') == 'response' and value.get('status') in ('completed', 'incomplete')
        else:
            value = request_profile(profile, '/chat/completions', data={'model': model, 'messages': [message], 'max_tokens': 64, 'stream': False})
            choices = value.get('choices')
            valid = isinstance(choices, list) and bool(choices) and isinstance(choices[0], dict) and isinstance(choices[0].get('message'), dict)
    if not valid:
        raise SetupError('The provider did not return a usable model response.')


def test(user, data):
    model = data.get('model')
    if not isinstance(model, str) or not 1 <= len(model.strip()) <= 200 or not model.strip().isprintable():
        raise SetupError('Enter a model ID of up to 200 printable characters.')
    model = model.strip()
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM checks WHERE at<?', (time.time() - 60,))
        if db.execute('SELECT COUNT(*) FROM checks WHERE owner=?', (user,)).fetchone()[0] >= 5:
            raise SetupError('Wait a minute before another model test.')
        db.execute('INSERT INTO checks VALUES (?,?)', (user, time.time()))
        state = read(db, user); profile = profile_for(user, state)
        model = bot_provider._model_name(model, profile['provider'])
        if not model: raise SetupError('Enter a model ID.')
        identity = fingerprint(profile, model)
        state.update(model=model, verified='', verified_at=0); write(db, user, state)
    probe(profile, model)  # One explicit, bounded request; no tools or user Chat data.
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        current = read(db, user); fresh = profile_for(user, current)
        if current.get('profile_id') != profile['id'] or current.get('model') != model or fingerprint(fresh, model) != identity:
            raise SetupError('Your provider settings changed during the test. Test the current selection again.')
        current.update(verified=identity, verified_at=time.time()); write(db, user, current)
    return status(user)


def complete(user):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        state = read(db, user)
        bot = existing_overwatcher(user)
        if bot:
            # Existing assistants are never reconfigured or restarted by setup.
            return status(user)
        profile = profile_for(user, state)
        if not verified(profile, state):
            raise SetupError('Test your current provider and model before creating the Overwatcher.')
        model = state['model']
        if model not in profile['models']:
            provider_profiles.save_profile(user, {**profile, 'models': [*profile['models'], model]})
        bot_id = state.get('bot_id') or 'overwatcher-' + uuid.uuid4().hex[:16]
        existing = chat_service.bots.load_bots().get(bot_id)
        if existing:
            raise SetupError('Setup found an existing Bot. Manage it in Bots before continuing.')
        rift = chat_service.bots.DATA_DIR / 'rifts' / bot_id
        rift.mkdir(parents=True, exist_ok=True)
        chat_service.bots.add_bot(bot_id, 'The Overwatcher', model, str(rift),
            policy=serve.coordinator_preset_policy(), status='running', owner=user,
            system_prompt=PROMPT, provider_profile_id=profile['id'])
        conv = chat_service.create_conversation(user, title='Welcome to Kyrex', bot_id=bot_id)
        chat_service._append_message(user, conv, 'assistant', WELCOME, identity='onboarding-welcome')
        chat_service._write(user, conv)
        state.update(bot_id=bot_id, conversation_id=conv['conversation_id'])
        write(db, user, state)
    return status(user)
