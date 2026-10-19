"""Owner-bound private handoff control; no Docker, sockets or external service."""
import json
import os
from pathlib import Path
import signal
import time
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import browser_reconnect as reconnect
import browser_reconnect_contract as contract
from browser_session_cookies import private_write


def spec(action='start', **changes):
    return {'owner': 'alice', 'bot_id': 'browser-a', 'action': action, 'request_id': 'a' * 32,
            'lease': '', 'url': 'https://fixture.test/sign-in' if action == 'start' else '', **changes}


class Child:
    def __init__(self): self.code = None; self.signals = []; self.pid = 12345
    def poll(self): return self.code
    def send_signal(self, sig): self.signals.append(sig)
    def wait(self, timeout=None): return self.code


class ReconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.calls = []; self.child = Child()
        def spawn(args, **kwargs): self.calls.append((args, kwargs)); return self.child
        self.supervisor = reconnect.Supervisor('alice', 'https://fixture.tail.ts.net/vnc.html', self.root, spawn=spawn)
    def tearDown(self): self.temp.cleanup()

    def test_private_url_accepts_only_operator_configured_tailnet_https(self):
        self.assertEqual(contract.viewer_url('https://fixture.ts.net'), 'https://fixture.ts.net/vnc.html')
        for url in ('https://evil.test', 'http://fixture.ts.net', 'https://fixture.ts.net.evil.test',
                    'https://user:pass@fixture.ts.net', 'https://fixture.ts.net/vnc.html?token=secret',
                    'https://fixture.ts.net:9999/vnc.html'):
            self.assertEqual(contract.viewer_url(url), '')

    def test_start_launches_only_fixed_hold_command_and_bound_profile(self):
        with patch.object(reconnect.manual_mode, 'is_any_active', return_value=[]):
            result = self.supervisor.handle(spec())
        self.assertEqual(result['state'], 'starting'); self.assertEqual(result['lease'], 'a' * 32)
        args, kw = self.calls[0]
        self.assertEqual(args[-1], 'hold'); self.assertEqual(len(args), 3)
        self.assertEqual(kw['env']['KYREX_VIEWER_OWNER'], 'alice')
        self.assertEqual(kw['env']['KYREX_VIEWER_BOT'], 'browser-a')
        self.assertEqual(kw['env']['KYREX_VIEWER_START_URL'], 'https://fixture.test/sign-in')
        self.assertEqual(kw['env']['KYREX_VIEWER_CONNECTOR'], '')

    def test_foreign_owner_cannot_launch_or_observe_window(self):
        self.assertEqual(self.supervisor.handle(spec(owner='bob'))['state'], 'unavailable')
        self.assertEqual(self.calls, [])

    def test_manual_or_automation_lock_refuses_launch(self):
        with patch.object(reconnect.manual_mode, 'is_any_active', return_value=[{'kind': 'automation'}]):
            self.assertEqual(self.supervisor.handle(spec())['state'], 'busy')
        self.assertEqual(self.calls, [])

    def test_end_requires_exact_bot_and_lease_and_cannot_stop_unrelated_viewer(self):
        with patch.object(reconnect.manual_mode, 'is_any_active', return_value=[]): self.supervisor.handle(spec())
        for bad in (spec('end', lease='b' * 32), spec('end', bot_id='browser-b', lease='a' * 32)):
            self.assertEqual(self.supervisor.handle(bad)['state'], 'unavailable')
        self.assertEqual(self.child.signals, [])
        self.assertEqual(self.supervisor.handle(spec('end', lease='a' * 32))['state'], 'stopping')
        self.assertEqual(self.child.signals, [signal.SIGTERM])

    def test_lost_acknowledgement_and_supervisor_restart_do_not_relaunch(self):
        directory = reconnect.root_for(self.root)
        private_write(directory, 'a' * 32 + '.request', json.dumps(spec()).encode())
        with patch.object(reconnect.manual_mode, 'is_any_active', return_value=[]): self.supervisor.tick()
        self.assertEqual(len(self.calls), 1)
        self.supervisor.tick(); self.assertEqual(len(self.calls), 1)
        (directory / ('a' * 32 + '.result')).unlink()  # crash between claim and acknowledgement
        self.supervisor.active = None; self.supervisor.child = None
        self.supervisor.tick(); self.assertEqual(len(self.calls), 1)
        self.assertEqual(json.loads((directory / ('a' * 32 + '.result')).read_text())['state'], 'unavailable')

    def test_stale_pending_request_never_launches_after_restart(self):
        directory = reconnect.root_for(self.root)
        path = directory / ('a' * 32 + '.request'); private_write(directory, path.name, json.dumps(spec()).encode())
        os.utime(path, (1, 1)); self.supervisor.tick()
        self.assertEqual(self.calls, [])

    def test_status_reads_heartbeat_without_new_command_or_replay(self):
        self.supervisor.tick()
        self.assertTrue(reconnect.ready('alice', self.root)); self.assertFalse(reconnect.ready('bob', self.root))
        self.assertEqual(reconnect.request(spec('status'), self.root)['state'], 'idle')
        self.assertEqual(list(reconnect.root_for(self.root).glob('*.request')), [])

    def test_result_projection_never_exposes_private_payload(self):
        result = contract.public_result({'state': 'ready', 'lease': 'a' * 32,
                                         'password': 'private-fixture', 'cookies': 'private-fixture'})
        self.assertNotIn('private-fixture', json.dumps(result))
        self.assertEqual(contract.public_result({'state': []})['state'], 'unavailable')


    def test_actual_viewer_launcher_retains_sandbox_and_has_no_tcp_debug_endpoint(self):
        import browser_operator
        import viewer_browser
        calls = []
        pw = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda *a, **kw: calls.append(kw)))
        with patch.object(browser_operator, '_managed_storage_options', return_value={}):
            viewer_browser.open_context(pw, '/profiles/fixture')
        opts = calls[0]
        self.assertIs(opts['chromium_sandbox'], True)
        self.assertIs(opts['headless'], False)
        self.assertFalse(any('remote-debugging' in a or a == '--no-sandbox' for a in opts['args']))
        self.assertIn('--enable-automation', opts['ignore_default_args'])
        self.assertIs(opts['handle_sigterm'], False)

    def host_agent(self):
        import agent
        config = agent.HostConfig(host_id='fixture-host', owner='alice', secret='fixture',
            cloud_url='wss://cloud.test', allowlist=['fixture.test'], profiles_root=str(self.root))
        host = agent.HostAgent(config, connect=lambda: None); host._authed = True
        frames = []; conn = SimpleNamespace(send=frames.append)
        return host, conn, frames

    def test_host_control_checks_auth_owner_freshness_and_host_site_permissions(self):
        host, conn, frames = self.host_agent()
        with patch.object(reconnect, 'request', return_value={'state': 'idle'}) as request:
            for body, issued_at in ((spec(owner='bob'), time.time()), (spec(), 1),
                                   (spec(url='https://outside.test/login'), time.time())):
                host._handle_reconnect(conn, body, issued_at=issued_at)
                self.assertEqual(frames[-1]['payload']['state'], 'unavailable')
            self.assertEqual(request.call_count, 0)
            host._authed = False; before = len(frames)
            host._handle_reconnect(conn, spec(), issued_at=time.time())
            self.assertEqual(len(frames), before)

    def test_host_admits_fresh_owner_control_but_start_is_refused_while_busy(self):
        host, conn, frames = self.host_agent()
        with patch.object(reconnect, 'request', return_value={'state': 'starting'}) as request:
            host._handle_reconnect(conn, spec(), issued_at=time.time())
            self.assertEqual(request.call_count, 1)
            host._handle_reconnect(conn, spec(), issued_at=time.time(), busy=True)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(frames[-1]['payload']['state'], 'busy')
            host._handle_reconnect(conn, spec('status'), issued_at=time.time(), busy=True)
            self.assertEqual(request.call_count, 2)


if __name__ == '__main__': unittest.main()
