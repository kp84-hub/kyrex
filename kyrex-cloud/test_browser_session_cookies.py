"""Real Fernet and filesystem tests; no external accounts or browser required."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from browser_session_cookies import COOKIE_FILE, KEY_FILE, MAX_AGE, SessionCookies, SessionCookieError, private_read, private_write


def cookie(**changes):
    return {'name': 'fixture-sid', 'value': 'opaque-fixture', 'domain': 'fixture.test',
            'path': '/', 'expires': -1, 'httpOnly': True, 'secure': True,
            'sameSite': 'Lax', **changes}


class Context:
    def __init__(self, values=()): self.values = list(values); self.added = []
    def cookies(self): return list(self.values)
    def add_cookies(self, values): self.added.extend(values); self.values.extend(values)


class SessionCookieTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = self.root / 'owner-a-bot-a'; self.profile.mkdir(mode=0o700)
        self.now = [1000.0]
    def tearDown(self): self.temp.cleanup()
    def keeper(self, context, profile=None):
        return SessionCookies(profile or self.profile, context, clock=lambda: self.now[0])

    def test_session_cookie_survives_new_context_encrypted_with_private_permissions(self):
        self.keeper(Context([cookie(), cookie(name='persistent', expires=99999)])).save(force=True)
        raw = (self.profile / COOKIE_FILE).read_bytes()
        self.assertNotIn(b'opaque-fixture', raw)
        self.assertNotIn(b'fixture.test', raw)
        for name in (COOKIE_FILE, KEY_FILE): self.assertEqual((self.profile / name).stat().st_mode & 0o777, 0o600)
        context = Context()
        self.assertEqual(self.keeper(context).restore(), 1)
        self.assertEqual(context.values, [cookie()])

    def test_newer_native_cookie_is_never_overwritten(self):
        self.keeper(Context([cookie()])).save(force=True)
        native = cookie(value='new-native', expires=99999)
        context = Context([native])
        self.assertEqual(self.keeper(context).restore(), 0)
        self.assertEqual(context.values, [native])

    def test_partition_keys_are_part_of_cookie_identity(self):
        a, b = cookie(partitionKey='https://studio-a.test'), cookie(partitionKey='https://studio-b.test')
        self.keeper(Context([a, b])).save(force=True)
        context = Context([a])
        self.assertEqual(self.keeper(context).restore(), 1)
        self.assertEqual(context.added, [b])

    def test_observed_logout_replaces_snapshot_with_empty_cookie_set(self):
        context = Context([cookie()]); keeper = self.keeper(context)
        keeper.save(force=True); context.values = []; keeper.save(force=True)
        self.assertEqual(self.keeper(Context()).restore(), 0)

    def test_expired_or_future_checkpoint_is_refused(self):
        self.keeper(Context([cookie()])).save(force=True)
        for clock in (1000 + MAX_AGE + 1, 999):
            with self.subTest(clock=clock):
                self.now[0] = clock
                context = Context()
                self.assertEqual(self.keeper(context).restore(), 0)
                self.assertEqual(context.added, [])

    def test_other_owner_and_bot_cannot_restore_copied_ciphertext(self):
        self.keeper(Context([cookie()])).save(force=True)
        for name in ('owner-b-bot-a', 'owner-a-bot-b'):
            other = self.root / name; other.mkdir(mode=0o700)
            self.assertEqual(self.keeper(Context(), other).restore(), 0)
            for file in (COOKIE_FILE, KEY_FILE): shutil.copy2(self.profile / file, other / file)
            context = Context()
            with self.assertRaises(SessionCookieError): self.keeper(context, other).restore()
            self.assertEqual(context.added, [])

    def test_missing_key_does_not_replace_vault_or_generate_a_new_identity(self):
        self.keeper(Context([cookie()])).save(force=True)
        old = (self.profile / COOKIE_FILE).read_bytes(); (self.profile / KEY_FILE).unlink()
        with self.assertRaises(SessionCookieError): self.keeper(Context()).restore()
        self.assertEqual((self.profile / COOKIE_FILE).read_bytes(), old)
        self.assertFalse((self.profile / KEY_FILE).exists())

    def test_unreadable_and_corrupt_storage_has_only_fixed_error_text(self):
        self.keeper(Context([cookie()])).save(force=True)
        (self.profile / COOKIE_FILE).write_bytes(b'private-cookie-value-corrupt')
        with self.assertRaises(SessionCookieError) as caught: self.keeper(Context()).restore()
        self.assertNotIn('private-cookie', str(caught.exception))

    def test_symlinks_and_world_readable_files_are_rejected(self):
        self.keeper(Context([cookie()])).save(force=True)
        path = self.profile / COOKIE_FILE; raw = path.read_bytes(); path.unlink()
        outside = self.root / 'outside'; outside.write_bytes(raw); outside.chmod(0o600)
        path.symlink_to(outside)
        with self.assertRaises(OSError): self.keeper(Context()).restore()
        path.unlink(); path.write_bytes(raw); path.chmod(0o644)
        with self.assertRaises(SessionCookieError): self.keeper(Context()).restore()

    def test_resetting_the_profile_removes_the_checkpoint(self):
        self.keeper(Context([cookie()])).save(force=True)
        shutil.rmtree(self.profile); self.profile.mkdir(mode=0o700)
        self.assertEqual(self.keeper(Context()).restore(), 0)

    def test_periodic_refresh_renews_checkpoint_timestamp_without_cookie_change(self):
        context = Context([cookie()]); keeper = self.keeper(context); keeper.save(force=True)
        for _ in range(125): self.now[0] += 30; keeper.save()
        self.now[0] = keeper.last_write + MAX_AGE - 1
        self.assertEqual(self.keeper(Context()).restore(), 1)

    def test_broken_context_does_not_overwrite_a_valid_snapshot(self):
        self.keeper(Context([cookie()])).save(force=True)
        before = (self.profile / COOKIE_FILE).read_bytes()
        class Broken:
            def cookies(self): raise RuntimeError('private provider value')
        with self.assertRaises(SessionCookieError) as caught: self.keeper(Broken()).save(force=True)
        self.assertNotIn('private provider', str(caught.exception))
        self.assertEqual((self.profile / COOKIE_FILE).read_bytes(), before)


    def test_manual_recovery_keeps_native_profile_and_can_save_fresh_sign_in(self):
        native = self.profile / 'native-profile-fixture'; native.write_bytes(b'native-cookie-data')
        self.keeper(Context([cookie()])).save(force=True)
        (self.profile / KEY_FILE).unlink()
        context = Context([cookie(value='fresh-human-sign-in')]); keeper = self.keeper(context)
        keeper.recover_for_manual_sign_in(); keeper.save(force=True)
        restored = Context(); self.keeper(restored).restore()
        self.assertEqual(restored.values, context.values)
        self.assertEqual(native.read_bytes(), b'native-cookie-data')


if __name__ == '__main__': unittest.main()
