"""Shared encrypted speech preferences; no wake audio or prompts are stored."""
import base64
import hashlib
import io
import json
import os
import sqlite3
import time
import wave
from pathlib import Path
import requests
from cryptography.fernet import Fernet, InvalidToken
from paths import data_dir


class SpeechError(ValueError):
    pass


class VoiceSpeech:
    MAX_AUDIO = 4 * 1024 * 1024
    VOICES = ('cedar', 'marin')

    def __init__(self):
        self.path = data_dir() / 'voice_speech.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS settings (owner TEXT PRIMARY KEY, sealed TEXT, engine TEXT, voice TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS usage (owner TEXT, at REAL)')
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
            return db.execute('SELECT sealed, engine, voice FROM settings WHERE owner=?', (owner,)).fetchone() or ('', 'device', 'cedar')

    def view(self, owner):
        sealed, engine, voice = self.record(owner)
        return {'configured': bool(sealed), 'engine': engine, 'voice': voice,
                'speech_model': 'gpt-4o-mini-tts', 'transcription_model': 'gpt-4o-mini-transcribe'}

    def configure(self, owner, data):
        if data.get('engine') not in ('openai', 'device') or data.get('voice') not in self.VOICES:
            raise SpeechError('Choose an AI voice or Device voice, and Cedar or Marin.')
        key = data.get('api_key', '')
        if not isinstance(key, str) or (key and (not 20 <= len(key) <= 512 or any(c.isspace() for c in key))):
            raise SpeechError('Enter a valid OpenAI API key.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT sealed FROM settings WHERE owner=?', (owner,)).fetchone()
            sealed = self.box().encrypt(key.encode()).decode() if key else (old[0] if old else '')
            if data.get('remove_key') is True:
                sealed = ''
            if data['engine'] == 'openai' and not sealed:
                raise SpeechError('Enter an OpenAI API key to enable the shared AI voice.')
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?,?,?)', (owner, sealed, data['engine'], data['voice']))
        return self.view(owner)

    def key(self, owner):
        sealed = self.record(owner)[0]
        if not sealed:
            raise SpeechError('Set up the OpenAI speech key in Kyrex Chat → Connections → Voice once for all your devices.')
        try:
            return self.box().decrypt(sealed.encode()).decode()
        except InvalidToken:
            raise SpeechError('The saved speech key is unavailable. Save it again in Voice settings.') from None

    def quota(self, owner):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM usage WHERE at<?', (time.time() - 60,))
            if db.execute('SELECT COUNT(*) FROM usage WHERE owner=?', (owner,)).fetchone()[0] >= 20:
                raise SpeechError('Voice is busy. Wait a minute before another request.')
            db.execute('INSERT INTO usage VALUES (?,?)', (owner, time.time()))

    def post(self, owner, endpoint, **kwargs):
        key = self.key(owner)
        self.quota(owner)
        try:
            response = requests.post('https://api.openai.com/v1/audio/' + endpoint,
                                     headers={'Authorization': 'Bearer ' + key}, timeout=(15, 45),
                                     allow_redirects=False, stream=True, **kwargs)
        except requests.RequestException:
            raise SpeechError('The speech provider could not be reached. No automatic retry was made.') from None
        if response.status_code != 200:
            response.close()
            if response.status_code in (401, 403):
                raise SpeechError('OpenAI rejected the speech key. Check Voice settings.')
            if response.status_code in (402, 429):
                raise SpeechError('OpenAI speech quota or rate limit reached. Check your API account.')
            raise SpeechError('The speech provider is unavailable. No automatic retry was made.')
        return response

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
        import array
        pcm = array.array('h', frames)
        if not pcm or max(abs(x) for x in pcm) < 100:
            raise SpeechError('No speech was heard. Try again after the tone.')
        response = self.post(owner, 'transcriptions', files={'file': ('command.wav', audio, 'audio/wav')},
                             data={'model': 'gpt-4o-mini-transcribe', 'language': 'en', 'response_format': 'json'})
        try:
            raw = self.bounded(response, 24000)
            text = json.loads(raw).get('text')
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                raise SpeechError('No usable command was recognized. Try again.')
            return {'text': text.strip()}
        except (ValueError, TypeError, AttributeError) as exc:
            if isinstance(exc, SpeechError):
                raise
            raise SpeechError('The speech provider returned an unreadable transcript.') from None
        finally:
            response.close()

    def speak(self, owner, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 1700:
            raise SpeechError('Voice needs a short reply.')
        if self.view(owner)['engine'] != 'openai':
            raise SpeechError('Device voice is selected. Choose an AI voice in Voice settings for the same voice on every device.')
        response = self.post(owner, 'speech', json={'model': 'gpt-4o-mini-tts', 'voice': self.view(owner)['voice'],
            'input': text, 'response_format': 'wav',
            'instructions': 'Speak naturally, warmly and clearly, with natural pauses and a normal pace. Read the supplied text without adding information.'})
        try:
            if not response.headers.get('Content-Type', '').startswith('audio/'):
                raise SpeechError('The speech provider returned an unexpected audio format.')
            return self.bounded(response, self.MAX_AUDIO)
        finally:
            response.close()

    @staticmethod
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
