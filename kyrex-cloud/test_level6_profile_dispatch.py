#!/usr/bin/env python3
"""Focused regression tests for Level 6 authorization/profile separation."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

os.environ.setdefault("WEB_SESSION_SECRET", "level6-profile-test-secret")

import browser_host_bridge as bridge  # noqa: E402
import browser_host_channel as channel  # noqa: E402
import level6_weekly  # noqa: E402
import serve  # noqa: E402
import task_store  # noqa: E402


OWNER = "alice"
AUTH_BOT = "level6-bot"
PROFILE_BOT = "browser-bot"
HOST = "host-1"


def _ctx(bot_id, *, owner=OWNER, policy=None, allowlist=None):
    return serve.ExecutionContext(
        session_id=bot_id,
        rift_path="/tmp/rift",
        bot_id=bot_id,
        bot_owner=owner,
        policy=(serve.level6_weekly_preset_policy()
                if policy is None else policy),
        browser_allowlist=(serve.level6_weekly_preset_allowlist()
                           if allowlist is None else allowlist),
    )


class _Channel:
    authenticated = True

    def __init__(self):
        self.calls = []

    def dispatch_task(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "ok"}


class Level6ProfileDispatchTests(unittest.TestCase):
    def _manager_call(self, *, task_text=None, profile_bot_id=PROFILE_BOT,
                      auth_ctx=None, profile_ctx=None):
        manager = channel.HostManager()
        live = _Channel()
        task_text = task_text or level6_weekly.weekly_browser_task_spec()
        auth_ctx = auth_ctx or _ctx(AUTH_BOT)
        profile_ctx = profile_ctx or _ctx(
            PROFILE_BOT, policy=serve.browser_preset_policy(),
            allowlist=["facebook.com"])

        def build_context(bot_id):
            return auth_ctx if bot_id == AUTH_BOT else profile_ctx

        host = SimpleNamespace(host_id=HOST, effective_state=lambda: "online")
        with (
            patch.object(serve, "build_context", side_effect=build_context),
            patch.object(channel._hosts, "host_for", return_value=host) as bound,
            patch.object(manager, "channel_for", return_value=live),
        ):
            result = manager.dispatch_browser_task(
                OWNER, AUTH_BOT, task_text,
                profile_bot_id=profile_bot_id,
            )
        return result, live, bound

    def test_level6_authorizes_with_selected_bot_but_uses_browser_profile(self):
        result, live, bound = self._manager_call()
        self.assertEqual(result, {"status": "ok"})
        bound.assert_called_once_with(OWNER, PROFILE_BOT)
        call = live.calls[0]
        self.assertEqual(call["bot_id"], PROFILE_BOT)
        self.assertEqual(call["policy"], serve.level6_weekly_preset_policy())
        self.assertEqual(call["allowlist"], ["facebook.com"])

    def test_split_identity_refuses_non_level6_task(self):
        with self.assertRaisesRegex(
                channel.ChannelError, "separate browser profile identity"):
            self._manager_call(task_text='{"navigate":"https://facebook.com"}')

    def test_split_identity_refuses_wrong_profile(self):
        with self.assertRaisesRegex(
                channel.ChannelError, "separate browser profile identity"):
            self._manager_call(profile_bot_id="some-other-profile")

    def test_split_identity_refuses_partial_policy(self):
        bad = _ctx(AUTH_BOT, policy={"browser:navigate": 0},
                   allowlist=["facebook.com"])
        with self.assertRaisesRegex(
                channel.ChannelError, "separate browser profile identity"):
            self._manager_call(auth_ctx=bad)

    def test_split_identity_refuses_foreign_profile_owner(self):
        foreign = _ctx(PROFILE_BOT, owner="mallory",
                       policy=serve.browser_preset_policy(),
                       allowlist=["facebook.com"])
        with self.assertRaisesRegex(
                channel.ChannelError, "not owned by you"):
            self._manager_call(profile_ctx=foreign)

    def test_level6_dispatch_keeps_authorization_context(self):
        auth = _ctx(AUTH_BOT)
        with patch.object(serve, "browser_host_dispatch",
                          return_value=({"ok": True}, None)) as dispatch:
            result = serve._level6_browser_dispatch(auth, "fixed")
        self.assertEqual(result, ({"ok": True}, None))
        dispatch.assert_called_once_with(
            auth, "fixed", on_progress=None, profile_bot_id=PROFILE_BOT)

    def test_durable_store_round_trips_both_identities(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = task_store.CloudTaskStore(Path(tmp) / "tasks.db")
            dispatch_id = store.submit_browser_dispatch(
                task_id="task-1", owner=OWNER, bot_id=AUTH_BOT,
                profile_bot_id=PROFILE_BOT, host_id=HOST,
                task_text=level6_weekly.weekly_browser_task_spec(),
            )
            row = store.get_browser_dispatch(dispatch_id)
        self.assertEqual(row["bot_id"], AUTH_BOT)
        self.assertEqual(row["profile_bot_id"], PROFILE_BOT)

    def test_legacy_dispatch_rows_default_profile_to_authorization_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = task_store.CloudTaskStore(Path(tmp) / "tasks.db")
            dispatch_id = store.submit_browser_dispatch(
                task_id="task-legacy", owner=OWNER, bot_id=AUTH_BOT,
                host_id=HOST, task_text="fixed",
            )
            row = store.get_browser_dispatch(dispatch_id)
        self.assertEqual(row["bot_id"], AUTH_BOT)
        self.assertEqual(row["profile_bot_id"], AUTH_BOT)

    def test_existing_database_migrates_profile_identity_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tasks.db"
            con = sqlite3.connect(db)
            con.execute("""
                CREATE TABLE browser_dispatches (
                    dispatch_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    owner TEXT NOT NULL, bot_id TEXT NOT NULL, host_id TEXT,
                    session_id TEXT, task_text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', claimed_by TEXT,
                    claim_token TEXT, claimed_at TEXT, result TEXT, error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    deadline_at TEXT, progress TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    finished_at TEXT
                )
            """)
            con.commit()
            con.close()
            store = task_store.CloudTaskStore(db)
            dispatch_id = store.submit_browser_dispatch(
                task_id="task-migrated", owner=OWNER, bot_id=AUTH_BOT,
                profile_bot_id=PROFILE_BOT, host_id=HOST, task_text="fixed",
            )
            row = store.get_browser_dispatch(dispatch_id)
        self.assertEqual(row["profile_bot_id"], PROFILE_BOT)

    def test_bridge_forwards_both_identities(self):
        class Manager:
            def __init__(self):
                self.call = None

            def dispatch_browser_task(self, owner, bot_id, task_text, **kwargs):
                self.call = (owner, bot_id, task_text, kwargs)
                return {"ok": True}

        manager = Manager()
        result, error = bridge._dispatch_via_manager(
            manager, OWNER, AUTH_BOT, PROFILE_BOT, "fixed", "session", None)
        self.assertIsNone(error)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(manager.call[1], AUTH_BOT)
        self.assertEqual(manager.call[3]["profile_bot_id"], PROFILE_BOT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
