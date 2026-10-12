"""Owner-scoped, encrypted speech settings shared by browser and native clients."""
import array
import base64
import hashlib
import io
import os
import sqlite3
import time
import wave

import requests  # Kept for transport injection by existing integrations/tests.
from cryptography.fernet import Fernet, InvalidToken
from paths import data_dir
import voice_speech_providers as providers
from voice_speech_providers import SpeechError


class VoiceSpeech:
    MAX_AUDIO = 4 * 1024 * 1024
    VOICES = providers.VOICES['openai']

    def __init__(self):
        self.path = data_dir() / 'voice_speech.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS settings (owner TEXT PRIMARY KEY, sealed TEXT, engine TEXT, voice TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS usage (owner TEXT, at REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS credentials (owner TEXT, provider TEXT, sealed TEXT, PRIMARY KEY (owner, provider))')
            db.execute('CREATE TABLE IF NOT EXISTS preferences (owner TEXT PRIMARY KEY, speech_provider TEXT, transcription_provider TEXT, transcription_model TEXT, speaking_rate REAL)')
            # Copy the existing encrypted OpenAI key without decrypting it or changing preferences.
            db.execute("INSERT OR IGNORE INTO credentials SELECT owner, 'openai', sealed FROM settings WHERE sealed != ''")
        self.path.chmod(0o600)

    def db(self):
        return sqlite3.connect(self.path, timeout=10)

    def box(self):
        secret = os.environ.get('WEB_SESSION_SECRET') or os.environ.get('KYREX_PROVIDER_SECRETS_KEY')
        if not secret:
            raise SpeechError('Voice credential encryption is unavailable on this server.')
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(('kyrex-voice-speech:' + secret).encode()).digest()))

    def record(self, owner):
        with self.db() as db:
            old = db.execute('SELECT engine, voice FROM settings WHERE owner=?', (owner,)).fetchone()
            pref = db.execute('SELECT speech_provider, transcription_provider, transcription_model, speaking_rate FROM preferences WHERE owner=?', (owner,)).fetchone()
        if old:
            speech, transcription, model, rate = pref or ('openai', 'openai', 'gpt-4o-mini-transcribe', 1.0)
            engine, voice = old
        else:
            engine, speech, voice = 'device', 'google', providers.DEFAULT_VOICES['google']
            transcription, model, rate = 'groq', 'whisper-large-v3', 1.0
        return {'engine': engine, 'voice': voice, 'speech_provider': speech,
                'transcription_provider': transcription, 'transcription_model': model,
                'speaking_rate': rate, 'saved': bool(old)}

    def credential_status(self, owner):
        with self.db() as db:
            present = dict(db.execute('SELECT provider, sealed FROM credentials WHERE owner=?', (owner,)).fetchall())
        return {provider: bool(present.get(provider)) for provider in providers.PROVIDER_NAMES}

    def view(self, owner):
        value = self.record(owner)
        status = self.credential_status(owner)
        transcription_ready = status[value['transcription_provider']]
        speech_ready = value['engine'] == 'device' or status[value['speech_provider']]
        return {**value, 'credentials': status, 'configured': transcription_ready and speech_ready,
                'transcription_configured': transcription_ready, 'speech_configured': speech_ready,
                'speech_model': providers.SPEECH_MODELS[value['speech_provider']],
                'options': {'voices': providers.VOICES, 'transcription_models': providers.TRANSCRIPTION_MODELS}}

    def configure(self, owner, data):
        old = self.record(owner)
        legacy = not any(k in data for k in ('speech_provider', 'transcription_provider', 'credentials', 'remove_credentials'))
        speech = 'openai' if legacy else data.get('speech_provider', old['speech_provider'])
        transcription = 'openai' if legacy else data.get('transcription_provider', old['transcription_provider'])
        engine = data.get('engine', old['engine'])
        if not all(isinstance(v, str) for v in (speech, transcription, engine)) or speech not in providers.VOICES or transcription not in providers.TRANSCRIPTION_MODELS or engine not in ('openai', 'device'):
            raise SpeechError('Choose a supported reply voice and transcription provider.')
        default_voice = old['voice'] if speech == old['speech_provider'] else providers.DEFAULT_VOICES[speech]
        voice = data.get('voice', default_voice)
        default_model = old['transcription_model'] if transcription == old['transcription_provider'] else providers.TRANSCRIPTION_MODELS[transcription][0]
        model = data.get('transcription_model', default_model)
        rate = data.get('speaking_rate', old['speaking_rate'])
        if not all(isinstance(v, str) for v in (voice, model)) or voice not in providers.VOICES[speech] or model not in providers.TRANSCRIPTION_MODELS[transcription]:
            raise SpeechError('Choose a voice and transcription model supported by their providers.')
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0.5 <= rate <= 2.0:
            raise SpeechError('Choose a speaking speed between 0.5 and 2.0.')
        incoming = data.get('credentials', {})
        removals = data.get('remove_credentials', [])
        if not isinstance(incoming, dict) or any(k not in providers.PROVIDER_NAMES for k in incoming):
            raise SpeechError('Choose supported speech credentials.')
        if not isinstance(removals, list) or any(not isinstance(k, str) or k not in providers.PROVIDER_NAMES for k in removals):
            raise SpeechError('Choose a saved speech credential to remove.')
        incoming = dict(incoming)
        if 'api_key' in data:
            incoming['openai'] = data['api_key']
        if data.get('remove_key') is True:
            removals = [*removals, 'openai']
        cleaned = {}
        for provider, value in incoming.items():
            if not isinstance(value, str):
                raise SpeechError('Enter valid speech credentials.')
            if not value:
                continue  # Blank inputs retain saved credentials.
            if provider == 'google':
                cleaned[provider] = providers.google_credentials(value)
            elif not 20 <= len(value) <= 512 or any(c.isspace() for c in value):
                raise SpeechError('Enter a valid ' + providers.PROVIDER_NAMES[provider] + ' API key.')
            else:
                cleaned[provider] = value
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for provider, value in cleaned.items():
                sealed = self.box().encrypt(value.encode()).decode()
                db.execute('INSERT OR REPLACE INTO credentials VALUES (?,?,?)', (owner, provider, sealed))
            for provider in removals:
                db.execute('DELETE FROM credentials WHERE owner=? AND provider=?', (owner, provider))
            # Retain the v0.4 wire marker engine="openai" for ANY shared/cloud reply voice.
            # Existing companions then keep calling the provider-neutral /api/voice/speech endpoint.
            # Clear the legacy column so a removed key cannot be reimported on the next request.
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?,?,?)', (owner, '', engine, voice))
            db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?,?,?,?)', (owner, speech, transcription, model, rate))
        return self.view(owner)

    def key(self, owner, provider='openai'):
        with self.db() as db:
            row = db.execute('SELECT sealed FROM credentials WHERE owner=? AND provider=?', (owner, provider)).fetchone()
        if not row or not row[0]:
            raise SpeechError('Set up ' + providers.PROVIDER_NAMES[provider] + ' in Kyrex Chat → Connections → Voice once for all your devices.')
        try:
            return self.box().decrypt(row[0].encode()).decode()
        except InvalidToken:
            raise SpeechError('The saved speech credentials are unavailable. Save them again in Voice settings.') from None

    def quota(self, owner):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM usage WHERE at<?', (time.time() - 60,))
            if db.execute('SELECT COUNT(*) FROM usage WHERE owner=?', (owner,)).fetchone()[0] >= 20:
                raise SpeechError('Voice is busy. Wait a minute before another request.')
            db.execute('INSERT INTO usage VALUES (?,?)', (owner, time.time()))

    def transcribe(self, owner, audio):
        try:
            with wave.open(io.BytesIO(audio)) as wav:
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000 or not 3200 <= wav.getnframes() <= 480000:
                    raise SpeechError('Speak one request of up to 30 seconds.')
                frames = wav.readframes(wav.getnframes())
                if len(frames) != wav.getnframes() * 2:
                    raise SpeechError('The command recording was incomplete.')
        except (wave.Error, EOFError):
            raise SpeechError('The command recording could not be read.') from None
        pcm = array.array('h', frames)
        if not pcm or max(abs(x) for x in pcm) < 100:
            raise SpeechError('No speech was heard. Try again after the tone.')
        value = self.record(owner)
        provider = value['transcription_provider']
        credential = self.key(owner, provider)
        self.quota(owner)
        return providers.transcribe(provider, credential, value['transcription_model'], audio)

    def speak(self, owner, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 1700 or len(text.encode()) > 5000:
            raise SpeechError('Voice needs a short reply.')
        value = self.record(owner)
        if value['engine'] == 'device':
            raise SpeechError('Device voice is selected. Choose a shared voice in Voice settings for the same voice on every device.')
        provider = value['speech_provider']
        credential = self.key(owner, provider)
        self.quota(owner)
        return providers.speak(provider, credential, value['voice'], value['speaking_rate'], text, self.MAX_AUDIO)

    bounded = staticmethod(providers.bounded)
