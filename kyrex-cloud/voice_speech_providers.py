"""Bounded speech adapters. Provider credentials and endpoints stay server-side."""
import base64
import binascii
import io
import json
import re
import time
import wave
from types import SimpleNamespace

import requests


class SpeechError(ValueError):
    pass


CHIRP_VOICES = ('Achernar', 'Achird', 'Algenib', 'Algieba', 'Alnilam', 'Aoede',
                'Autonoe', 'Callirrhoe', 'Charon', 'Despina', 'Enceladus', 'Erinome',
                'Fenrir', 'Gacrux', 'Iapetus', 'Kore', 'Laomedeia', 'Leda', 'Orus',
                'Pulcherrima', 'Puck', 'Rasalgethi', 'Sadachbia', 'Sadaltager',
                'Schedar', 'Sulafat', 'Umbriel', 'Vindemiatrix', 'Zephyr', 'Zubenelgenubi')
VOICES = {'google': tuple('en-US-Chirp3-HD-' + name for name in CHIRP_VOICES),
          'openai': ('cedar', 'marin')}
DEFAULT_VOICES = {'google': 'en-US-Chirp3-HD-Leda', 'openai': 'cedar'}
TRANSCRIPTION_MODELS = {'groq': ('whisper-large-v3', 'whisper-large-v3-turbo'),
                        'openai': ('gpt-4o-mini-transcribe',)}
SPEECH_MODELS = {'google': 'Chirp 3 HD', 'openai': 'gpt-4o-mini-tts'}
PROVIDER_NAMES = {'google': 'Google Cloud', 'groq': 'Groq', 'openai': 'OpenAI'}
OAUTH_URL = 'https://oauth2.googleapis.com/token'


def google_credentials(value):
    """Accept service-account keys only; never accept credential-supplied URLs."""
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 16000:
            raise ValueError()
        info = json.loads(value)
        if not isinstance(info, dict) or info.get('type') != 'service_account':
            raise ValueError()
        if info.get('token_uri', OAUTH_URL) != OAUTH_URL:
            raise ValueError()
        if not isinstance(info.get('project_id'), str) or not re.fullmatch(r'[a-z][a-z0-9-]{4,61}[a-z0-9]', info['project_id']):
            raise ValueError()
        if not isinstance(info.get('client_email'), str) or not re.fullmatch(r'[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.iam\.gserviceaccount\.com', info['client_email']):
            raise ValueError()
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
        key = load_pem_private_key(info['private_key'].encode(), password=None)
        if not isinstance(key, RSAPrivateKey) or key.key_size < 2048:
            raise ValueError()
        # Drop optional URL fields and all unrelated Firebase/Google configuration.
        clean = {k: info[k] for k in ('type', 'project_id', 'client_email', 'private_key')}
        clean['token_uri'] = OAUTH_URL
        return json.dumps(clean)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SpeechError('Enter a valid Google Cloud service account JSON key for Text-to-Speech.') from None


def bounded(response, maximum):
    out = bytearray()
    deadline = time.monotonic() + 90
    try:
        for chunk in response.iter_content(8192):
            out.extend(chunk)
            if len(out) > maximum or time.monotonic() > deadline:
                raise SpeechError('The speech response exceeded its limit.')
    except requests.RequestException:
        raise SpeechError('The speech connection was interrupted. Check your voice chat before repeating a command.') from None
    if not out:
        raise SpeechError('The speech provider returned no audio or transcript.')
    return bytes(out)


def post(provider, url, headers, **kwargs):
    try:
        response = requests.post(url, headers=headers, timeout=(15, 45),
                                 allow_redirects=False, stream=True, **kwargs)
    except requests.RequestException:
        raise SpeechError('The speech provider could not be reached. No automatic retry was made.') from None
    if response.status_code == 200:
        return response
    name = PROVIDER_NAMES[provider]
    try:
        if response.status_code in (401, 403):
            detail = ' Check that Text-to-Speech is enabled and the service account has access.' if provider == 'google' else ' Check Voice settings.'
            raise SpeechError(name + ' rejected the speech credentials or access.' + detail)
        if response.status_code in (402, 429):
            try:
                error = json.loads(bounded(response, 16000)).get('error', {})
                code = error.get('code') or error.get('type')
            except (ValueError, TypeError, AttributeError):
                code = None
            if code in ('insufficient_quota', 'credit_balance_exhausted') or response.status_code == 402:
                raise SpeechError(name + ' speech credits or quota are exhausted. Check API billing; waiting will not restore access.')
            if code in ('organization_spend_limit_exceeded', 'project_spend_limit_exceeded', 'organization_usage_limit_exceeded'):
                raise SpeechError(name + ' speech account limit reached. Check API account limits.')
            delay = response.headers.get('Retry-After', '')
            wait = f' Wait at least {delay} seconds before trying again.' if re.fullmatch(r'\d{1,4}', delay) else ' Wait before trying again.'
            raise SpeechError(name + ' speech request limited. Check account quota.' + wait)
        raise SpeechError(name + ' speech is unavailable. No automatic retry was made.')
    finally:
        response.close()


class GoogleTokenRequest:
    """google-auth transport with one fixed, bounded OAuth request and no retries."""
    def __init__(self):
        self.used = False

    def __call__(self, url, method='GET', body=None, headers=None, **kwargs):
        from google.auth.exceptions import TransportError
        if self.used or url != OAUTH_URL or method != 'POST':
            raise TransportError('Google speech authentication was not retried.')
        self.used = True
        response = None
        try:
            response = requests.post(OAUTH_URL, data=body, headers=headers, timeout=(10, 15),
                                     allow_redirects=False, stream=True)
            return SimpleNamespace(status=response.status_code, data=bounded(response, 16000), headers=response.headers)
        except (requests.RequestException, SpeechError):
            raise TransportError('Google speech authentication could not be reached.') from None
        finally:
            if response is not None:
                response.close()


def google_headers(credential):
    from google.oauth2 import service_account
    from google.auth.exceptions import GoogleAuthError
    try:
        credentials = service_account.Credentials.from_service_account_info(
            json.loads(credential), scopes=['https://www.googleapis.com/auth/cloud-platform'])
        credentials.refresh(GoogleTokenRequest())
        return {'Authorization': 'Bearer ' + credentials.token,
                'x-goog-user-project': credentials.project_id}
    except (GoogleAuthError, ValueError, TypeError, KeyError):
        raise SpeechError('Google Cloud speech authentication failed. Check the service account key in Voice settings.') from None


def transcribe(provider, credential, model, audio):
    base = {'openai': 'https://api.openai.com/v1', 'groq': 'https://api.groq.com/openai/v1'}[provider]
    response = post(provider, base + '/audio/transcriptions', {'Authorization': 'Bearer ' + credential},
                    files={'file': ('command.wav', audio, 'audio/wav')},
                    data={'model': model, 'language': 'en', 'response_format': 'json'})
    try:
        text = json.loads(bounded(response, 24000)).get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise SpeechError('No usable command was recognized. Try again.')
        return {'text': text.strip()}
    except (ValueError, TypeError, AttributeError) as exc:
        if isinstance(exc, SpeechError):
            raise
        raise SpeechError('The speech provider returned an unreadable transcript.') from None
    finally:
        response.close()


def speak(provider, credential, voice, rate, text, maximum):
    if provider == 'google':
        response = post(provider, 'https://texttospeech.googleapis.com/v1/text:synthesize', google_headers(credential),
                        json={'input': {'text': text}, 'voice': {'languageCode': 'en-US', 'name': voice},
                              'audioConfig': {'audioEncoding': 'LINEAR16', 'sampleRateHertz': 24000, 'speakingRate': rate}})
    else:
        response = post(provider, 'https://api.openai.com/v1/audio/speech', {'Authorization': 'Bearer ' + credential},
                        json={'model': SPEECH_MODELS[provider], 'voice': voice, 'input': text,
                              'response_format': 'wav', 'speed': rate,
                              'instructions': 'Speak naturally, warmly and clearly, with natural pauses and a normal pace. Read the supplied text without adding information.'})
    try:
        if provider == 'google':
            # Google returns base64 LINEAR16 with a WAV header, not an audio HTTP body.
            raw = json.loads(bounded(response, maximum * 4 // 3 + 24000)).get('audioContent')
            audio = base64.b64decode(raw, validate=True)
            if len(audio) > maximum:
                raise SpeechError('The speech response exceeded its limit.')
            with wave.open(io.BytesIO(audio)) as wav:
                if wav.getsampwidth() != 2 or wav.getnchannels() != 1 or wav.getnframes() < 1:
                    raise ValueError()
                if len(wav.readframes(wav.getnframes())) != wav.getnframes() * 2:
                    raise ValueError()
            return audio
        if not response.headers.get('Content-Type', '').startswith('audio/'):
            raise SpeechError('The speech provider returned an unexpected audio format.')
        return bounded(response, maximum)
    except (ValueError, TypeError, AttributeError, binascii.Error, wave.Error, EOFError) as exc:
        if isinstance(exc, SpeechError):
            raise
        raise SpeechError('The speech provider returned unreadable audio.') from None
    finally:
        response.close()
