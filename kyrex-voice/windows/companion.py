"""Kyrex Voice for Windows. No microphone starts until an explicit user action."""
import base64
import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import wave
import webbrowser
from client import Client, VoiceError, command_wav

VERSION = '0.5'


def protect(data, decrypt=False):
    if os.name != 'nt':
        raise VoiceError('The Windows companion requires Windows.')
    class Blob(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))); target = Blob()
    crypt = ctypes.windll.crypt32; kernel = ctypes.windll.kernel32
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise VoiceError('Windows could not unlock the voice pairing. Pair this PC again.')
    try:
        return ctypes.string_at(target.data, target.length)
    finally:
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


class Account:
    def __init__(self):
        self.directory = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'KyrexVoice'
        self.path = self.directory / 'pairing.json'

    def load(self):
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text())
            return {'origin': value['origin'], 'token': protect(base64.b64decode(value['sealed']), True).decode()}
        except (ValueError, KeyError, OSError):
            raise VoiceError('Saved pairing is unavailable. Pair this PC again.') from None

    def save(self, origin, token):
        self.directory.mkdir(parents=True, exist_ok=True)
        value = {'origin': origin, 'sealed': base64.b64encode(protect(token.encode())).decode()}
        with tempfile.NamedTemporaryFile('w', dir=self.directory, delete=False, encoding='utf-8') as out:
            json.dump(value, out); temporary = out.name
        os.replace(temporary, self.path)

    def forget(self):
        self.path.unlink(missing_ok=True)


def assets():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'wake'


def spotter():
    import sherpa_onnx
    root = assets()
    return sherpa_onnx.KeywordSpotter(tokens=str(root / 'tokens.txt'), encoder=str(root / 'encoder.onnx'),
        decoder=str(root / 'decoder.onnx'), joiner=str(root / 'joiner.onnx'), keywords_file=str(root / 'keywords.txt'),
        keywords_threshold=0.15, keywords_score=1.5, num_trailing_blanks=2, num_threads=1)


class Controller:
    def __init__(self, update, account):
        self.update = update; self.account = account; self.stopped = threading.Event(); self.thread = None
        self.client = None

    def stop(self):
        self.stopped.set()
        if os.name == 'nt':
            import winsound
            winsound.PlaySound(None, 0)
        self.update('Stopped. Sent requests can still finish in Chat.')

    def start(self, wake):
        if self.thread and self.thread.is_alive():
            self.update('Wait for the previous voice session to finish stopping.'); return
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, args=(wake, self.stopped), daemon=True)
        self.thread.start()

    def heartbeat(self, client, stopped):
        while not stopped.wait(15):
            try:
                client.json('heartbeat')
            except Exception:
                stopped.set(); self.update('Voice moved to another device or disconnected. Start listening again.'); return

    def run(self, wake, stopped):
        import numpy as np
        import sounddevice as sd
        client = None
        try:
            saved = self.account.load()
            if not saved:
                raise VoiceError('Pair this PC from Kyrex Chat → Connections → Voice first.')
            client = Client(saved['origin'], saved['token']); self.client = client
            session = client.json('session', method='GET')
            if not session['settings']['configured']:
                raise VoiceError('Set up the speech key in Kyrex Chat → Connections → Voice once for all devices.')
            if stopped.is_set(): return
            client.claim()
            threading.Thread(target=self.heartbeat, args=(client, stopped), daemon=True).start()
            kws = spotter() if wake else None
            following_up = 0
            while not stopped.is_set():
                if wake and not following_up:
                    self.update('Listening for Hey Kyrex')
                    stream = kws.create_stream()
                    with sd.RawInputStream(samplerate=16000, channels=1, dtype='int16', blocksize=1600) as mic:
                        while not stopped.is_set():
                            data, overflow = mic.read(1600)
                            if overflow: raise VoiceError('The microphone fell behind. Stop other recording apps and try again.')
                            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768
                            self.update(None, level=int(np.max(np.abs(samples)) * 100))
                            stream.accept_waveform(16000, samples)
                            while kws.is_ready(stream):
                                kws.decode_stream(stream)
                            if kws.get_result(stream): break
                if stopped.is_set(): return
                frames = self.record_command(stopped, following_up)
                if not frames:
                    if stopped.is_set(): return
                    if following_up:
                        following_up = 0
                        if wake: continue
                        self.update('Voice off'); break
                    raise VoiceError('No speech was heard. Try Talk now again.')
                if stopped.is_set(): return
                self.update('Recognizing your request…')
                text = client.json('transcribe', data=command_wav(frames), headers={'Content-Type': 'audio/wav'})['text']
                if stopped.is_set(): return
                self.update('Kyrex is working…', transcript=text)
                reply = client.ask(text, stopped)
                if stopped.is_set() or reply is None: return
                settings = client.json('settings', method='GET')
                if stopped.is_set(): return
                self.update('Preparing the reply…')
                if settings['engine'] == 'openai':
                    audio = client.speech(reply)
                    if stopped.is_set(): return
                    self.play(audio, stopped)
                else:
                    self.speak_device(reply, stopped)
                following_up = settings.get('follow_up_seconds', 8)
                if type(following_up) is not int or following_up not in (8, 15): following_up = 0
                if not wake and not following_up: break
        except Exception as error:
            if not stopped.is_set():
                self.update(str(error) if isinstance(error, VoiceError) else 'Voice could not start or complete. Check microphone access and your voice chat.')
        finally:
            stopped.set()
            if client: client.stop()
            self.client = None


    def record_command(self, stopped, follow_up_seconds=0):
        import numpy as np
        import sounddevice as sd
        import winsound
        if follow_up_seconds and stopped.wait(.25): return None
        frames = []; voiced = 0; silence = 0
        wait_frames = (follow_up_seconds or 8) * 10
        if stopped.is_set(): return None
        winsound.Beep(880, 120)
        with sd.RawInputStream(samplerate=16000, channels=1, dtype='int16', blocksize=1600) as mic:
            if stopped.is_set(): return None
            self.update('Listening for your follow-up' if follow_up_seconds else 'Speak after the tone')
            for _ in range(300):
                if stopped.is_set(): return None
                data, overflow = mic.read(1600)
                if overflow: raise VoiceError('Command recording was interrupted. Try again.')
                data = bytes(data); frames.append(data)
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768
                self.update(None, level=int(np.max(np.abs(samples)) * 100))
                if np.sqrt(np.mean(samples * samples)) >= 0.008:
                    voiced += 1600; silence = 0
                else: silence += 1600
                if voiced >= 2400 and silence >= 19200: break
                if voiced < 2400 and len(frames) >= wait_frames: return None
        return frames if voiced >= 2400 else None

    def play(self, audio, stopped):
        import winsound
        with wave.open(io.BytesIO(audio)) as wav:
            duration = wav.getnframes() / wav.getframerate()
        if duration > 120: raise VoiceError('Reply audio was too long. Read it in Chat.')
        with tempfile.TemporaryDirectory(prefix='kyrex-voice-') as directory:
            path = Path(directory) / 'reply.wav'; path.write_bytes(audio)
            if stopped.is_set(): return
            self.update('Speaking the reply')
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            stopped.wait(duration + .1)
            winsound.PlaySound(None, 0)

    def speak_device(self, text, stopped):
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        try:
            speaker = comtypes.client.CreateObject('SAPI.SpVoice'); self.update('Speaking the reply')
            if stopped.is_set(): return
            speaker.Speak(text, 1)
            while not speaker.WaitUntilDone(100):
                if stopped.is_set(): speaker.Speak('', 3); break
        finally:
            comtypes.CoUninitialize()


def main():
    import tkinter as tk
    from tkinter import ttk, messagebox
    from concurrent.futures import ThreadPoolExecutor
    import pystray
    from PIL import Image, ImageDraw
    account = Account(); events = queue.Queue(); executor = ThreadPoolExecutor(max_workers=1)
    root = tk.Tk(); root.title('Kyrex Voice v' + VERSION); root.geometry('480x540')
    status = tk.StringVar(value='Voice off'); transcript = tk.StringVar(); origin = tk.StringVar(value='https://chat.kyrex.dev'); code = tk.StringVar(); level = tk.IntVar()
    try:
        saved = account.load()
        if saved: origin.set(saved['origin']); status.set('Paired. Start listening or Talk now.')
    except VoiceError as error: status.set(str(error))
    controller = Controller(lambda value=None, **extra: events.put((value, extra)), account)
    def task(function):
        def run():
            try: function()
            except Exception as error: events.put((str(error) if isinstance(error, VoiceError) else 'Pairing failed. Generate a new code in Kyrex Chat.', {}))
        executor.submit(run)
    def pair():
        try: client = Client(origin.get())
        except VoiceError as error: status.set(str(error)); return
        pairing = code.get(); code.set('')
        def run():
            value = client.pair(pairing); account.save(client.origin, value['device_token']); events.put(('Paired. Start listening or Talk now.', {}))
        task(run)
    def open_chat(settings=False):
        from client import checked_origin
        try: webbrowser.open(checked_origin(origin.get()) + ('/?settings=voice' if settings else '/'))
        except VoiceError as error: status.set(str(error))
    frame = ttk.Frame(root, padding=20); frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='Kyrex Voice · PC & phone', font=('Segoe UI', 17)).pack(anchor='w')
    ttk.Label(frame, text='Say Hey Kyrex, wait for the tone, then speak.\nAfter replies, wait for the beep to continue.\nWake audio stays on this PC. Command audio goes\nto your shared speech provider.', wraplength=430).pack(anchor='w', pady=12)
    ttk.Label(frame, textvariable=status, wraplength=430, font=('Segoe UI', 11)).pack(anchor='w', pady=8)
    ttk.Progressbar(frame, maximum=100, variable=level).pack(fill='x')
    ttk.Label(frame, textvariable=transcript, wraplength=430).pack(anchor='w', pady=8)
    ttk.Label(frame, text='Kyrex HTTPS server').pack(anchor='w'); ttk.Entry(frame, textvariable=origin).pack(fill='x')
    ttk.Label(frame, text='Pairing code from Connections → Voice → Pair Windows PC').pack(anchor='w', pady=(10, 0)); ttk.Entry(frame, textvariable=code, show='•').pack(fill='x')
    ttk.Button(frame, text='Link this PC', command=pair).pack(fill='x', pady=5)
    row = ttk.Frame(frame); row.pack(fill='x', pady=6)
    ttk.Button(row, text='Start listening', command=lambda: controller.start(True)).pack(side='left', expand=True, fill='x')
    ttk.Button(row, text='Talk now', command=lambda: controller.start(False)).pack(side='left', expand=True, fill='x')
    ttk.Button(row, text='Stop', command=controller.stop).pack(side='left', expand=True, fill='x')
    ttk.Button(frame, text='Shared voice settings', command=lambda: open_chat(True)).pack(fill='x', pady=4)
    ttk.Button(frame, text='Open Kyrex Chat', command=open_chat).pack(fill='x', pady=4)
    def forget():
        controller.stop(); account.forget(); status.set('Pairing removed. Disconnect this PC in Chat to revoke it.')
    ttk.Button(frame, text='Forget pairing', command=forget).pack(fill='x', pady=4)
    image = Image.new('RGBA', (64, 64), '#181828'); ImageDraw.Draw(image).text((19, 13), 'K', fill='#bdacff', stroke_width=1)
    def show(): root.after(0, lambda: (root.deiconify(), root.lift()))
    def quit_app(): controller.stop(); icon.stop(); root.after(0, root.destroy); executor.shutdown(wait=False, cancel_futures=True)
    icon = pystray.Icon('KyrexVoice', image, 'Kyrex Voice — stopped', pystray.Menu(
        pystray.MenuItem('Open Kyrex Voice', lambda *_: show(), default=True),
        pystray.MenuItem('Start listening', lambda *_: controller.start(True)),
        pystray.MenuItem('Talk now', lambda *_: controller.start(False)),
        pystray.MenuItem('Stop listening', lambda *_: controller.stop()),
        pystray.MenuItem('Quit', lambda *_: quit_app())))
    threading.Thread(target=icon.run, daemon=True).start()
    root.protocol('WM_DELETE_WINDOW', root.withdraw)
    ttk.Button(frame, text='Hide to tray', command=root.withdraw).pack(fill='x', pady=4)
    def refresh():
        while True:
            try: value, extra = events.get_nowait()
            except queue.Empty: break
            if value:
                status.set(value); icon.title = ('Kyrex Voice — ' + value)[:120]
            if 'transcript' in extra: transcript.set('Last heard: ' + extra['transcript'][:300])
            if 'level' in extra: level.set(extra['level'])
        root.after(100, refresh)
    refresh(); root.mainloop()


if __name__ == '__main__':
    if '--self-test' in sys.argv:
        import numpy, sounddevice, sherpa_onnx, tkinter, pystray, PIL, comtypes
        payload = b'test-only-voice-pairing'
        assert protect(protect(payload), True) == payload
        engine = spotter(); assert engine.create_stream() is not None
        result = Path(sys.argv[sys.argv.index('--self-test') + 1])
        result.write_text('Windows DPAPI, wake model, audio, tray and UI imports passed; no microphone opened.')
    else:
        main()
