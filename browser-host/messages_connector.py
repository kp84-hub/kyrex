"""Owner-controlled Google pairing; Bots only get bounded, read-only results.

Dedicated profile: never borrows a Calendar Bot's cookies. No public listener,
CDP address, arbitrary URL, script, file path, or send operation is exposed.
All Playwright calls run on one thread. Pairing input stops after verification.
"""
import base64
import json
import os
import queue
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import manual_mode
import profiles

URL = 'https://messages.google.com/web/'
TTL = 900
THREADS = 'mws-conversation-list-item a[href*="/conversations/"], a[href^="/web/conversations/"]'
PAIRED = 'mws-conversations-list, mws-conversation-list, mws-conversation-list-item'
BUBBLES = 'mws-message-wrapper'


def safe_navigation(url):
    p = urlsplit(url)
    return (p.scheme == 'https' and p.hostname in {'messages.google.com', 'accounts.google.com'}
            and p.port in {None, 443} and not p.username and not p.password)


def validate(action, data):
    if action not in {'connect', 'screen', 'input', 'finish', 'status', 'read', 'disconnect'}:
        raise ValueError('Unsupported Messages operation')
    if action == 'input':
        kind = data.get('kind')
        if kind == 'click':
            for k, maximum in [('x', 1024), ('y', 768)]:
                v = data.get(k)
                if isinstance(v, bool) or not isinstance(v, (float, int)) or not 0 <= v < maximum:
                    raise ValueError('Invalid screen position')
        elif kind == 'text':
            if not isinstance(data.get('text'), str) or len(data['text']) > 500:
                raise ValueError('Input is too long')
        elif kind == 'key':
            if data.get('key') not in {'Enter', 'Tab', 'Backspace', 'Escape', 'ArrowUp', 'ArrowDown', 'Control+A'}:
                raise ValueError('Unsupported key')
        elif kind == 'scroll':
            if data.get('delta') not in {-500, 500}:
                raise ValueError('Invalid scroll')
        else:
            raise ValueError('Unsupported input')
    if action == 'read':
        if not isinstance(data.get('query', ''), str) or len(data.get('query', '')) > 200:
            raise ValueError('Query is too long')


class MessagesBrowser:
    def __init__(self, owner, root=None):
        self.owner = owner
        self.root = root
        # Hash the full owner identity, rather than truncating/slugging it.
        self.path = profiles.messages_profile_dir(owner, root=root)
        self.context = self.page = self.pw = self.lock = None
        self.deadline = 0
        self.pairing = False
        self.verified = False

    def close(self, delete=False):
        # Disconnect must also respect a human using this profile. Cloud has
        # already revoked consent; report cleanup pending rather than erase a
        # running manual browser's files.
        if delete and not self.lock:
            self.lock = self.acquire_lock()
        try:
            if self.context:
                self.context.close()
            if delete and self.path.exists():
                shutil.rmtree(self.path)
        finally:
            self.context = self.page = None
            try:
                if self.pw:
                    self.pw.stop()
            finally:
                self.pw = None
                if self.lock:
                    self.lock.release()
                    self.lock = None
        self.verified = self.pairing = False

    def acquire_lock(self):
        return manual_mode.acquire(*profiles.messages_lock_key(self.owner),
                                   kind=manual_mode.KIND_AUTOMATION,
                                   ttl=manual_mode.AUTOMATION_MAX_TTL,
                                   root=manual_mode.state_dir(profiles_root=self.root or profiles.profiles_root()))

    def open(self):
        if self.context:
            return
        from playwright.sync_api import sync_playwright
        # Use the same lock boundary as the existing viewer and automation.
        self.lock = self.acquire_lock()
        try:
            self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.chmod(0o700)
            self.pw = sync_playwright().start()
            self.context = self.pw.chromium.launch_persistent_context(
                str(self.path), executable_path=os.environ.get('KYREX_BROWSER_EXECUTABLE', '/usr/bin/google-chrome-stable'),
                headless=os.environ.get('KYREX_BROWSER_HEADLESS', '0') != '0',
                viewport={'width': 1024, 'height': 768}, accept_downloads=False,
                args=['--disable-dev-shm-usage', '--no-first-run', '--no-default-browser-check'] + (['--no-sandbox'] if os.geteuid() == 0 else []))
            # Only Messages and Google sign-in may be navigated. Subresources
            # need Google's static/CDN hosts; main-frame navigations are strict.
            def guard(route):
                req = route.request
                if req.is_navigation_request() and not safe_navigation(req.url):
                    route.abort()
                else:
                    route.continue_()
            self.context.route('**/*', guard)
            self.context.on('page', self.new_page)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(3000)
            self.page.goto(URL, wait_until='commit', timeout=20000)
        except Exception:
            self.close()
            raise

    def new_page(self, page):
        self.page = page
        page.set_default_timeout(3000)

    def paired(self):
        return (self.page and urlsplit(self.page.url).hostname == 'messages.google.com'
                and '/welcome' not in self.page.url
                and self.page.locator(PAIRED).count() > 0 and self.page.locator(PAIRED).first.is_visible())

    def screen(self):
        if not self.pairing or time.time() >= self.deadline:
            raise ValueError('Pairing expired. Tap Connect again.')
        if not safe_navigation(self.page.url):
            raise ValueError('Pairing left Google Messages')
        return {'image': base64.b64encode(self.page.screenshot(type='jpeg', quality=65)).decode(),
                'width': 1024, 'height': 768, 'ready': bool(self.paired())}

    def read(self, query):
        if not self.verified:
            raise ValueError('Finish connecting Messages first')
        self.page.goto(URL, wait_until='commit', timeout=15000)
        try:
            self.page.locator(PAIRED).first.wait_for(state='visible', timeout=10000)
        except Exception:
            self.verified = False
            raise ValueError('Google Messages needs reconnecting. Tap Connect in Connections.')
        # Read visible text from up to ten recent conversations. This bounded
        # view is intentionally explicit; it is not an exhaustive inbox search.
        urls = self.page.locator(THREADS).evaluate_all('(nodes) => [...new Set(nodes.map(n => n.href))].slice(0,10)')
        if not urls and self.page.locator('mws-conversation-list-item').count():
            raise ValueError('Google Messages layout changed; reading is unavailable')
        messages = []
        deadline = time.monotonic() + 20
        for url in urls:
            if time.monotonic() >= deadline: break
            p = urlsplit(url)
            if not safe_navigation(url) or p.hostname != 'messages.google.com' or not p.path.startswith('/web/conversations/'):
                continue
            self.page.goto(url, wait_until='commit', timeout=2000)
            try:
                self.page.locator(BUBBLES).first.wait_for(state='visible', timeout=3000)
            except Exception:
                continue
            title = self.page.locator('h1, h2, mws-conversation-header').first
            sender = title.inner_text()[:200] if title.count() else 'Conversation'
            texts = self.page.locator(BUBBLES).all_inner_texts()[-10:]
            messages.extend({'sender': sender, 'number': '', 'received': '', 'body': t[:10000]} for t in reversed(texts) if t.strip())
        terms = query.casefold().split()
        matches = [m for m in messages if all(t in (m['sender'] + ' ' + m['body']).casefold() for t in terms)]
        return {'messages': matches[:10], 'checked_at': time.time(), 'conversation_count': len(urls), 'bounded': True}

    def command(self, action, data):
        validate(action, data)
        if action == 'disconnect':
            self.close(delete=True)
            return {'disconnected': True}
        self.open()
        if action == 'status':
            try:
                self.page.locator(PAIRED).first.wait_for(state='visible', timeout=3000)
            except Exception:
                pass
            self.verified = bool(self.paired())
            return {'connected': self.verified}
        if action == 'connect':
            self.pairing = True
            self.deadline = time.time() + TTL
            return self.screen()
        if action == 'finish':
            if not self.pairing or time.time() >= self.deadline or not self.paired():
                raise ValueError('Confirm pairing in Google Messages on your phone first')
            self.verified = True
            self.pairing = False
            return {'connected': True}
        if action == 'read':
            # Cloud only dispatches read with a durable owner consent record.
            # Re-verify the persisted profile after a host restart.
            if not self.verified and self.paired():
                self.verified = True
            return self.read(data.get('query', ''))
        if not self.pairing or time.time() >= self.deadline:
            raise ValueError('Pairing expired. Tap Connect again.')
        # Pairing input only: never drive a paired message composer.
        parts = urlsplit(self.page.url)
        login_page = parts.hostname == 'accounts.google.com' or (parts.hostname == 'messages.google.com' and parts.path.startswith(('/web/welcome', '/web/authentication')))
        if action == 'input' and not self.paired() and login_page:
            kind = data['kind']
            if kind == 'click': self.page.mouse.click(data['x'], data['y'])
            elif kind == 'text': self.page.keyboard.insert_text(data['text'])
            elif kind == 'key': self.page.keyboard.press(data['key'])
            elif kind == 'scroll': self.page.mouse.wheel(0, data['delta'])
        return self.screen()


class MessagesWorker:
    """Serialize browser work independently of the agent's task receive loop."""
    def __init__(self, owner, root, emit):
        self.browser = MessagesBrowser(owner, root)
        self.emit = emit
        self.stopped = threading.Event()
        self.q = queue.Queue(maxsize=8)
        self.thread = threading.Thread(target=self.run, daemon=True, name='messages-connector')
        self.thread.start()

    def submit(self, payload):
        try:
            self.q.put_nowait(payload)
        except queue.Full:
            self.emit({'request_id': payload.get('request_id'), 'error': 'Messages is busy. Try again.'})

    def run(self):
        while not self.stopped.is_set():
            try:
                payload = self.q.get(timeout=15)
            except queue.Empty:
                if self.browser.pairing and time.time() >= self.browser.deadline:
                    self.browser.close()
                continue
            if payload is None:
                self.browser.close()
                return
            result = {'request_id': payload.get('request_id')}
            try:
                if payload.get('owner') != self.browser.owner:
                    raise ValueError('Messages owner mismatch')
                wire = payload.get('data_wire', '')
                if not isinstance(wire, str) or len(wire) > 4000:
                    raise ValueError('Invalid Messages request')
                data = json.loads(base64.b64decode(wire, validate=True))
                if not isinstance(data, dict):
                    raise ValueError('Invalid Messages request')
                result['result_wire'] = base64.b64encode(json.dumps(self.browser.command(payload.get('action'), data)).encode()).decode()
            except ValueError as exc:
                result['error'] = str(exc)
            except Exception:
                # Never echo browser exceptions: they can contain typed text,
                # cookies, URLs with auth codes, or message bodies.
                self.browser.close()
                result['error'] = 'Messages browser unavailable. Reconnect or try again.'
            try:
                self.emit(result)
            except Exception:
                self.stopped.set()
        self.browser.close()

    def stop(self):
        self.stopped.set()
        try: self.q.put_nowait(None)
        except queue.Full: pass
