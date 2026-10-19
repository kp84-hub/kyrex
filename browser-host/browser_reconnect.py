"""Private viewer supervisor and a bounded local mailbox for the enrolled agent.

No Docker socket, command text, public listener or viewer traffic through Cloud.
The supervisor runs as the existing non-root viewer user. It only launches the
existing, TTL-bound hold() command against a validated owner/Bot profile. The
same kernel profile lock arbitrates with automation and other manual viewers.
"""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import browser_reconnect_contract as contract
from browser_session_cookies import private_read, private_write
import manual_mode
import profiles

DIRECTORY = '.kyrex-browser-reconnect'
MAX_RECORDS = 200
RECORD_SECONDS = 86400


def root_for(root=None, *, create=True):
    path = profiles.profiles_root(root) / DIRECTORY
    if path.is_symlink(): raise ValueError('Invalid browser reconnect storage.')
    if create: path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077: raise ValueError('Browser reconnect storage is not private.')
    return path


def ready(owner, root=None):
    try:
        data = json.loads(private_read(root_for(root, create=False), 'ready.json', 4096))
        return (data.get('owner') == owner and 0 <= time.time() - data['at'] < 10
                and bool(contract.viewer_url(data.get('viewer_url'))))
    except Exception: return False


def request(value, root=None, *, timeout=6):
    spec = contract.command(value)
    if not ready(spec['owner'], root): return contract.public_result({'state': 'unavailable'})
    directory = root_for(root)
    if spec['action'] == 'status':
        status = json.loads(private_read(directory, 'ready.json', 8192))
        return contract.public_result(status.get('active') if status.get('bot_id') == spec['bot_id'] else {'state': 'idle'})
    if len(list(directory.glob('*.request'))) >= MAX_RECORDS:
        return contract.public_result({'state': 'unavailable'})
    ident = spec['request_id']
    payload = json.dumps(spec, sort_keys=True).encode()
    request_name = ident + '.request'
    try: private_write(directory, request_name, payload, replace=False)
    except FileExistsError:
        if private_read(directory, request_name, 8192) != payload:
            raise ValueError('Browser reconnect identity already has different details.') from None
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            result = json.loads(private_read(directory, ident + '.result', 8192))
            return contract.public_result(result)
        except FileNotFoundError: time.sleep(.1)
    return contract.public_result({'state': 'pending'})


class Supervisor:
    def __init__(self, owner, url, root=None, *, spawn=subprocess.Popen):
        if not owner or not contract.viewer_url(url): raise ValueError('Configure the private browser viewer URL.')
        self.owner, self.url = owner, contract.viewer_url(url)
        self.root = root_for(root)
        self.profiles_root = profiles.profiles_root(root)
        self.spawn = spawn
        self.child = None
        self.active = None
        self.last_heartbeat = 0

    def state(self, bot):
        if not self.active or self.active['bot_id'] != bot: return {'state': 'idle'}
        if self.child.poll() is not None:
            self.child.wait()
            result = {'state': 'ended', 'lease': self.active['request_id']}
            self.active = None
            self.child = None
            return result
        record = manual_mode.active(self.owner, bot, profiles_root=self.profiles_root)
        reachable = False
        if record and record.get('kind') == manual_mode.KIND_MANUAL and record.get('pid') == self.child.pid:
            try:
                port = int(record.get('novnc_port', 6080))
                if 1 <= port <= 65535:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1): reachable = True
            except (OSError, ValueError): pass
        return {'state': 'ready' if reachable else 'starting',
                'lease': self.active['request_id'], 'viewer_url': self.url}

    def handle(self, raw):
        spec = contract.command(raw)
        if spec['owner'] != self.owner: return {'state': 'unavailable'}
        state = self.state(spec['bot_id'])
        if spec['action'] == 'status': return state
        if spec['action'] == 'end':
            if not self.active or spec['lease'] != self.active['request_id'] or spec['bot_id'] != self.active['bot_id']:
                return {'state': 'unavailable'}
            if self.child.poll() is None: self.child.send_signal(signal.SIGTERM)
            return {'state': 'stopping', 'lease': spec['lease']}
        if self.active:
            return {'state': 'busy'}
        state_root = manual_mode.state_dir(profiles_root=self.profiles_root)
        if manual_mode.is_any_active(root=state_root): return {'state': 'busy'}
        env = os.environ.copy()
        env.update(KYREX_VIEWER_OWNER=self.owner, KYREX_VIEWER_BOT=spec['bot_id'],
                   KYREX_VIEWER_TTL='2700', KYREX_VIEWER_START_URL=spec['url'], KYREX_VIEWER_CONNECTOR='')
        # No command or path comes from the request. hold() acquires the same
        # lock as the agent before launching any browser or viewer listener.
        self.child = self.spawn([sys.executable, str(Path(__file__).with_name('viewer_ctl.py')), 'hold'],
                                env=env, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.active = spec
        return {'state': 'starting', 'lease': spec['request_id'], 'viewer_url': self.url}

    def tick(self):
        now = time.time()
        if now - self.last_heartbeat >= 2:
            state = self.state(self.active['bot_id']) if self.active else {'state': 'idle'}
            private_write(self.root, 'ready.json', json.dumps({'owner': self.owner, 'at': now,
                'viewer_url': self.url, 'active': state,
                'bot_id': self.active['bot_id'] if self.active else ''}).encode())
            self.last_heartbeat = now
        if self.active: self.state(self.active['bot_id'])
        records = sorted(self.root.glob('*.request'), key=lambda p: p.stat().st_mtime)
        for path in records[:MAX_RECORDS]:
            if not contract.REQUEST.fullmatch(path.stem): continue
            result_name = path.stem + '.result'
            if (self.root / result_name).exists():
                if now - path.stat().st_mtime > RECORD_SECONDS:
                    path.unlink()
                    (self.root / result_name).unlink()
                    try: (self.root / (path.stem + '.claim')).unlink()
                    except FileNotFoundError: pass
                continue
            try:
                spec = json.loads(private_read(self.root, path.name, 8192))
                # Claim before launching or stopping anything. A supervisor
                # crash between this write and the result is never replayed.
                try: private_write(self.root, path.stem + '.claim', b'claimed', replace=False)
                except FileExistsError:
                    private_write(self.root, result_name, json.dumps({'state': 'unavailable'}).encode())
                    continue
                # A stale start is never executed after reconnect or restart.
                result = {'state': 'unavailable'} if now - path.stat().st_mtime > 15 else self.handle(spec)
            except Exception: result = {'state': 'unavailable'}
            private_write(self.root, result_name, json.dumps(contract.public_result(result)).encode())

    def close(self):
        if self.child and self.child.poll() is None:
            self.child.send_signal(signal.SIGTERM)
            try: self.child.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait(timeout=5)
        try: (self.root / 'ready.json').unlink()
        except FileNotFoundError: pass


def serve():
    if os.getuid() == 0: raise ValueError('The private reconnect supervisor must be non-root.')
    supervisor = Supervisor(os.environ.get('KYREX_HOST_OWNER', ''), os.environ.get('KYREX_PRIVATE_VIEWER_URL', ''))
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopped:
            supervisor.tick()
            time.sleep(.2)
    finally: supervisor.close()


if __name__ == '__main__':
    try: serve()
    except Exception:
        print('Private browser reconnect service is unavailable; check its owner and private URL configuration.', file=sys.stderr)
        raise SystemExit(1) from None
