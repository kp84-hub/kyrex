"""Real root -> profile-owner workers; no live credentials or accounts."""
import hashlib
import errno
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import profile_identity as identity


class RepairBoundaryTests(unittest.TestCase):
    def test_unmanaged_child_drops_transport_secrets_without_changing_parent(self):
        original = {'HOME': '/original', 'KYREX_HOST_ENROLLMENT_SECRET': 'fixture-only'}
        child, options = identity.operator_options(original)
        self.assertEqual((child, options), ({'HOME': '/original'}, {}))
        self.assertIn('KYREX_HOST_ENROLLMENT_SECRET', original)

    def test_repair_refuses_a_foreign_enrolled_owner_before_locking(self):
        import manual_mode
        with patch.dict(os.environ, {'KYREX_HOST_OWNER': 'alice'}), \
             patch.object(sys, 'argv', ['repair', '--owner', 'bob', '--bot', 'browser']), \
             patch.object(manual_mode, 'acquire') as acquire:
            with self.assertRaises(ValueError): identity.main()
            acquire.assert_not_called()

    def test_repair_cannot_run_while_the_profile_lock_is_held(self):
        import manual_mode
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'KYREX_HOST_OWNER': 'alice', 'KYREX_BROWSER_PROFILES_ROOT': directory,
                                        'KYREX_VIEWER_STATE_DIR': directory + '/state'}), \
                 patch.object(sys, 'argv', ['repair', '--owner', 'alice', '--bot', 'browser']), \
                 patch.object(identity, 'repair_control_files') as repair:
                with manual_mode.acquire('alice', 'browser', kind=manual_mode.KIND_AUTOMATION):
                    with self.assertRaises(manual_mode.ManualControlActive): identity.main()
                repair.assert_not_called()


class ProfileIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        self.profile = self.root / 'profile'
        self.profile.mkdir(mode=0o700)
        self.uid = 1000 if os.geteuid() == 0 else os.geteuid()
        try:
            self.account = pwd.getpwuid(self.uid)
        except KeyError:
            self.skipTest('A non-root runtime account is required.')
        if os.geteuid() == 0:
            try:
                os.chown(self.profile, self.uid, self.account.pw_gid)
            except OSError as exc:
                if exc.errno in (errno.EINVAL, errno.EPERM):
                    self.skipTest('This user namespace cannot map the profile UID; real UID tests run in the agent image.')
                raise
        self.env = dict(os.environ, KYREX_BROWSER_SESSION_DIR=str(self.profile))
        for key in ('KYREX_FS_ROOT', 'KYREX_BROWSER_ROOT'):
            self.env.pop(key, None)

    def test_identity_comes_from_profile_and_parent_environment_is_unchanged(self):
        self.env.update(KYREX_HOST_ENROLLMENT_SECRET='fixture-transport-value',
                        KYREX_VIEWER_VNC_PASSWORD='fixture-password', HOME='/root')
        environment, options = identity.operator_options(self.env)
        self.assertEqual(environment['HOME'], self.account.pw_dir)
        self.assertEqual(self.env['HOME'], '/root')
        self.assertNotIn('KYREX_HOST_ENROLLMENT_SECRET', environment)
        self.assertNotIn('KYREX_VIEWER_VNC_PASSWORD', environment)
        workspace = Path(environment['KYREX_FS_ROOT'])
        self.assertEqual(workspace.parent, self.profile)
        self.assertEqual(workspace.stat().st_uid, self.uid)
        self.assertEqual(workspace.stat().st_mode & 0o777, 0o700)
        if os.geteuid() == 0:
            self.assertEqual(options, {'user': self.uid, 'group': self.account.pw_gid,
                                      'extra_groups': [], 'umask': 0o077})

    def test_explicit_workspace_is_preserved(self):
        self.env['KYREX_FS_ROOT'] = '/explicit/operator/workspace'
        env, _ = identity.operator_options(self.env)
        self.assertEqual(env['KYREX_FS_ROOT'], self.env['KYREX_FS_ROOT'])
        self.assertFalse((self.profile / '.kyrex-operator-files').exists())

    def test_profile_symlink_is_refused(self):
        link = self.root / 'alias'
        link.symlink_to(self.profile, target_is_directory=True)
        with self.assertRaises(ValueError):
            identity.operator_options(dict(self.env, KYREX_BROWSER_SESSION_DIR=str(link)))

    def test_shared_or_symlinked_artifact_directory_is_refused_without_chmod(self):
        work = self.profile / '.kyrex-operator-files'
        work.mkdir(mode=0o755)
        if os.geteuid() == 0:
            os.chown(work, self.uid, self.account.pw_gid)
        with self.assertRaises(ValueError):
            identity.operator_options(self.env)
        self.assertEqual(work.stat().st_mode & 0o777, 0o755)
        work.rmdir(); work.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            identity.operator_options(self.env)

    def test_other_nonroot_identity_cannot_take_the_profile(self):
        with patch.object(identity.os, 'geteuid', return_value=self.uid + 1):
            with self.assertRaises(ValueError):
                identity.operator_options(self.env)

    def test_missing_account_or_home_refuses_without_fallback_to_root(self):
        with patch.object(pwd, 'getpwuid', side_effect=KeyError):
            with self.assertRaises(ValueError): identity.operator_options(self.env)
        with patch.object(pwd, 'getpwuid', return_value=SimpleNamespace(pw_dir=str(self.root / 'missing'))):
            with self.assertRaises(ValueError): identity.operator_options(self.env)

    def test_root_only_and_unmanaged_execution_remain_compatible(self):
        env, options = identity.operator_options({'HOME': '/original'})
        self.assertEqual((env, options), ({'HOME': '/original'}, {}))
        if os.geteuid() == 0:
            root_profile = self.root / 'legacy-root'; root_profile.mkdir()
            env, options = identity.operator_options({'HOME': '/root', 'KYREX_BROWSER_SESSION_DIR': str(root_profile)})
            self.assertEqual(options, {}); self.assertEqual(env['HOME'], '/root')

    def test_profiles_get_distinct_private_artifact_directories(self):
        other = self.root / 'other'; other.mkdir(mode=0o700)
        if os.geteuid() == 0: os.chown(other, self.uid, self.account.pw_gid)
        a, _ = identity.operator_options(self.env)
        b, _ = identity.operator_options(dict(self.env, KYREX_BROWSER_SESSION_DIR=str(other)))
        self.assertNotEqual(a['KYREX_FS_ROOT'], b['KYREX_FS_ROOT'])

    @unittest.skipUnless(os.geteuid() == 0, 'Real UID transition requires root; also runs in the built agent image.')
    def test_real_agent_worker_creates_private_files_as_profile_owner(self):
        from agent import SubprocessExecutor
        script = self.root / 'operator_fixture.py'
        script.write_text('''import os,json
from pathlib import Path
p=Path(os.environ['KYREX_BROWSER_SESSION_DIR'])
for name in ('Local State','Preferences'):
 t=p/(name+'.tmp'); t.write_text('fixture'); t.replace(p/name)
print('KYREX_RESULT_JSON:'+json.dumps({'uid':os.geteuid(),'groups':os.getgroups(),
 'transport_secret_present':'KYREX_HOST_ENROLLMENT_SECRET' in os.environ}),flush=True)
''')
        script.chmod(0o644)
        self.env['KYREX_HOST_ENROLLMENT_SECRET'] = 'fixture-transport-value'
        for _ in range(2):
            worker = SubprocessExecutor('fixture', self.env, script=str(script))
            worker.start()
            try:
                result = worker.read()['result']
                self.assertEqual(result['uid'], self.uid)
                self.assertEqual(result['groups'], [])
                self.assertFalse(result['transport_secret_present'])
                self.assertIsNone(worker.read())
            finally:
                self.assertEqual(worker.stop(), 0)
            for name in ('Local State', 'Preferences'):
                self.assertEqual((self.profile / name).stat().st_uid, self.uid)
                self.assertEqual((self.profile / name).stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.geteuid() == 0, 'Metadata repair requires root.')
    def test_repair_changes_only_two_control_file_owners_and_retains_bytes_and_mode(self):
        (self.profile / 'Default').mkdir()
        for name in (*identity.CONTROL_FILES, 'untouched'):
            path = self.profile / name; path.write_bytes(b'fixture-native-state'); path.chmod(0o600)
        before = {name: hashlib.sha256((self.profile / name).read_bytes()).hexdigest() for name in identity.CONTROL_FILES}
        self.assertEqual(identity.repair_control_files(self.profile), list(identity.CONTROL_FILES))
        for name in identity.CONTROL_FILES:
            path = self.profile / name
            self.assertEqual(path.stat().st_uid, self.uid)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before[name])
        self.assertEqual((self.profile / 'untouched').stat().st_uid, 0)
        self.assertEqual(identity.repair_control_files(self.profile), [])

    @unittest.skipUnless(os.geteuid() == 0, 'Metadata repair requires root.')
    def test_repair_refuses_symlinks_and_hardlinks(self):
        outside = self.root / 'outside'; outside.write_text('fixture')
        (self.profile / 'Local State').symlink_to(outside)
        with self.assertRaises(OSError): identity.repair_control_files(self.profile)
        self.assertEqual(outside.stat().st_uid, 0)
        (self.profile / 'Local State').unlink()
        os.link(outside, self.profile / 'Local State')
        with self.assertRaises(ValueError): identity.repair_control_files(self.profile)
        self.assertEqual(outside.stat().st_uid, 0)


@unittest.skipUnless(os.environ.get('KYREX_TEST_PROFILE_IDENTITY_BROWSER') == '1' and os.geteuid() == 0,
                     'Real Chrome UID handoff runs in the actual agent image.')
class ChromeIdentityTests(unittest.TestCase):
    setUp = ProfileIdentityTests.setUp
    def test_real_chrome_storage_survives_viewer_uid_and_agent_worker_handoff(self):
        from agent import SubprocessExecutor
        script = self.root / 'chrome_fixture.py'
        script.write_text('''import os,sys,json
from browser_operator import PlaywrightDriver
d=PlaywrightDriver(os.environ['KYREX_BROWSER_SESSION_DIR'],managed=True)
try:
 d.open()
 d._context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body='<h1>Fixture</h1>'))
 d.navigate('https://identity.fixture.test/sign-in')
 if sys.argv[-1]=='write':
  d._page.evaluate("localStorage.setItem('fixture_login','present')")
  d._context.add_cookies([{'name':'fixture_session','value':'fixture','domain':'identity.fixture.test','path':'/','secure':True,'httpOnly':True,'sameSite':'Lax'}])
 storage=d._page.evaluate("localStorage.getItem('fixture_login')==='present'")
 cookie=any(c['name']=='fixture_session' for c in d._context.cookies())
 result={'uid':os.geteuid(),'storage':storage,'cookie':cookie}
finally:d.close()
print('KYREX_RESULT_JSON:'+json.dumps(result),flush=True)
''')
        script.chmod(0o644)
        env = dict(self.env, KYREX_BROWSER_HEADLESS='1', PYTHONPATH='/host')
        # The private viewer's actual UID/home, then root agent -> dropped worker,
        # then the private viewer's UID/home again. Fixture page traffic is intercepted.
        direct_env = dict(env, HOME=self.account.pw_dir)
        for mode, via_agent in (('write', False), ('read', True), ('read', False), ('read', True)):
            if via_agent:
                worker = SubprocessExecutor(mode, env, script=str(script)); worker.start()
                try:
                    frame = worker.read()
                    while frame and frame['kind'] != 'result': frame = worker.read()
                    self.assertIsNotNone(frame); result = frame['result']
                finally:self.assertEqual(worker.stop(), 0)
            else:
                proc = subprocess.run([sys.executable,str(script),mode],env=direct_env,
                    user=self.uid,group=self.account.pw_gid,extra_groups=[],umask=0o077,
                    text=True,capture_output=True,timeout=60)
                self.assertEqual(proc.returncode,0,'Fixture Chrome failed: '+proc.stderr[-500:])
                result=json.loads(next(l.split(':',1)[1] for l in proc.stdout.splitlines() if l.startswith('KYREX_RESULT_JSON:')))
            self.assertEqual(result,{'uid':self.uid,'storage':True,'cookie':True})
            self.assertEqual((self.profile/'Local State').stat().st_uid,self.uid)
            self.assertEqual((self.profile/'Default/Preferences').stat().st_uid,self.uid)


if __name__ == '__main__':
    program = unittest.main(exit=False)
    required = os.environ.get('KYREX_TEST_PROFILE_IDENTITY_BROWSER') == '1'
    raise SystemExit(0 if program.result.wasSuccessful() and not (required and program.result.skipped) else 1)
