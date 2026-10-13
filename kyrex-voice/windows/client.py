"""Bounded native client; one submission, no automatic network retries."""
import io
import json
import time
import uuid
import wave
from urllib.parse import urlsplit
import requests


class VoiceError(ValueError):
    pass


class VoiceConnectionUnavailable(VoiceError):
    """A transient transport/service failure; heartbeat may renew the same lease."""


class VoiceSessionLost(VoiceError):
    """The server rejected the lease/pairing. Never reclaim automatically."""


def checked_origin(value):
    url = urlsplit(value.strip())
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise VoiceError('Enter the Kyrex HTTPS server without a path.')
    return value.strip().rstrip('/')


def spoken_text(raw):
    import re
    result = re.sub(r'```[\s\S]*?```', ' Details are in your voice chat. ', str(raw or ''))
    result = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', result)
    result = re.sub(r'[*#`|]', '', result)
    result = re.sub(r'\s+', ' ', result).strip()
    return (result[:1600] + '. The full response is in your voice chat.') if len(result) > 1600 else result or 'Open your voice chat to review the result.'


def command_wav(frames):
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(b''.join(frames))
    return output.getvalue()


class Client:
    def __init__(self, origin, token='', transport=None):
        self.origin = checked_origin(origin); self.token = token; self.lease = ''
        self.transport = transport or requests.Session()
        # Trust the system CA but do not pick up ambient proxy credentials.
        if hasattr(self.transport, 'trust_env'):
            self.transport.trust_env = False

    def request(self, path, **kwargs):
        headers = {'Authorization': 'Bearer ' + self.token} if self.token else {}
        if self.lease:
            headers['X-Voice-Lease'] = self.lease
        headers.update(kwargs.pop('headers', {}))
        try:
            response = self.transport.request(kwargs.pop('method', 'POST'), self.origin + '/api/voice/' + path,
                headers=headers, timeout=(5, 5) if path in ('heartbeat', 'stop') else (15, 45), allow_redirects=False, **kwargs)
        except requests.exceptions.SSLError:
            raise VoiceError('The secure voice connection could not be verified. Check the Kyrex server address.') from None
        except requests.RequestException:
            raise VoiceConnectionUnavailable('Voice connection failed. Check your voice chat before repeating a request.') from None
        if response.status_code != 200:
            if path == 'heartbeat':
                code = response.status_code
                reason = response.headers.get('X-Voice-Reason', '')
                response.close()
                if code in (408, 429) or 500 <= code <= 599:
                    raise VoiceConnectionUnavailable('Voice connection interrupted. Reconnecting…')
                messages = {
                    'moved': 'Listening switched to another voice session. Stop browser/phone voice, then Start listening on this PC.',
                    'stopped': 'The voice listening session was stopped. Start listening again.',
                    'expired': 'The listening session expired after a connection loss. Start listening again.',
                }
                if code in (401, 403):
                    raise VoiceSessionLost('This PC pairing is unavailable. Pair Windows PC again in Chat → Connections → Voice.')
                raise VoiceSessionLost(messages.get(reason, 'The voice session is unavailable. Check Voice in Chat, then Start listening again.'))
            try:
                message = response.json().get('detail', 'Voice is unavailable. Check your voice chat.')
            except (ValueError, AttributeError):
                message = 'Voice is unavailable. Check your voice chat.'
            response.close()
            raise VoiceError(str(message)[:500])
        return response

    def json(self, path, **kwargs):
        response = self.request(path, **kwargs)
        try:
            return response.json()
        finally:
            response.close()

    def pair(self, code):
        value = self.json('pair', json={'pairing_code': code.strip()})
        self.token = value['device_token']
        return value

    def claim(self):
        self.lease = self.json('listen', json={'name': 'Windows PC', 'takeover': True})['lease']

    def stop(self):
        if self.lease:
            try:
                self.json('stop')
            except VoiceError:
                pass
            self.lease = ''

    def ask(self, text, stopped):
        if stopped.is_set():
            return None
        response = self.request('chat', json={'message': text, 'request_id': uuid.uuid4().hex}, stream=True)
        deadline = time.monotonic() + 300
        try:
            reply = None
            for line in response.iter_lines(chunk_size=1):
                if stopped.is_set():
                    return None
                if time.monotonic() > deadline:
                    raise VoiceError('The request is saved in voice chat. Check its progress there.')
                if len(line) > 65536:
                    raise VoiceError('Voice received an unreadable response. Check Chat.')
                if not line.startswith(b'data: '):
                    continue
                event = json.loads(line[6:])
                if event.get('type') == 'done':
                    reply = spoken_text(event.get('content'))
                if event.get('type') == 'approval_request':
                    reply = 'This needs your approval. Open your voice chat in Kyrex to review it.'
                if event.get('type') == 'error':
                    raise VoiceError('Kyrex reported an error. Check voice chat before repeating the request.')
            if not reply:
                raise VoiceError('The connection ended. Check voice chat before repeating the request.')
            return reply
        finally:
            response.close()

    def speech(self, text):
        response = self.request('speech', json={'text': text}, stream=True)
        try:
            if not response.headers.get('Content-Type', '').startswith('audio/'):
                raise VoiceError('The reply audio was unreadable. Read it in voice chat.')
            audio = bytearray(); deadline = time.monotonic() + 90
            for chunk in response.iter_content(8192):
                audio.extend(chunk)
                if len(audio) > 4 * 1024 * 1024 or time.monotonic() > deadline:
                    raise VoiceError('The reply audio exceeded its limit. Read it in voice chat.')
            return bytes(audio)
        finally:
            response.close()
