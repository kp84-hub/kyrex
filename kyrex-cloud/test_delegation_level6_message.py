"""Chief may delegate exact Level 6 commands without a target Rift/provider."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bots
import delegation
import serve
from task_store import CloudTaskStore


class ChiefLevel6DelegationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        registry = patch.object(bots, "BOTS_FILE", str(root / "bots.json"))
        registry.start()
        self.addCleanup(registry.stop)
        self.store = CloudTaskStore(db_path=root / "tasks.db")
        self.addCleanup(self.store.close)
        self.chief = bots.add_bot(
            "chief", "Chief", "", "", policy=serve.COORDINATOR_PRESET,
            status="running", owner="alice", provider_profile_id="")
        self.calendar = bots.add_bot(
            "calendar", "Calendar", "", "", policy=serve.CALENDAR_PRESET,
            status="running", owner="alice", provider_profile_id="")
        self.other = bots.add_bot(
            "other", "Other", "", "", policy={},
            status="running", owner="alice", provider_profile_id="")

    def test_preview_uses_calendar_executor_and_preserves_full_answer(self):
        self.assertTrue(delegation.safe_bot_metadata(self.calendar)["available"])
        view = delegation.submit_delegation(
            "alice", self.chief, "calendar", "#L6Workout preview",
            store=self.store, parent_conversation_id="conversation-1")
        task = self.store.get(view["task_id"])
        self.assertEqual(task["executor_prefix"], "level6")
        self.assertEqual(task["task_text"], serve.LEVEL6_MESSAGE_PREVIEW_REQUEST)
        self.assertEqual(task["bot_id"], "calendar")
        self.assertEqual(task["chat_id"], "alice")
        self.assertFalse(task.get("repo_url"))
        self.assertEqual(task["parent_delegation_id"], view["delegation_id"])
        preview = "Preview only — nothing sent.\n" + "workout\n" * 120
        self.assertEqual(serve.format_result({
            "status": "no_changes", "mode": "level6_preview",
            "final_response": preview}), preview.strip())

    def test_invalid_commands_and_wrong_target_fail_before_record(self):
        for target, command in (
            ("other", "#L6Workout preview"),
            ("calendar", "#L6Workout preview please"),
            ("calendar", "#L6Workout send to someone else"),
        ):
            with self.subTest(target=target, command=command):
                with self.assertRaises(delegation.DelegationError):
                    delegation.submit_delegation(
                        "alice", self.chief, target, command, store=self.store)
        self.assertEqual(self.store.list_delegations(owner="alice"), [])

    def test_send_and_test_respect_setting_preview_does_not(self):
        with patch.dict(os.environ, {"KYREX_LEVEL6_SEND_ENABLED": "0"}):
            for command in ("#L6Workout", "#L6Workout test"):
                with self.subTest(command=command):
                    with self.assertRaisesRegex(delegation.DelegationError, "disabled"):
                        delegation.submit_delegation(
                            "alice", self.chief, "calendar", command,
                            store=self.store)
            self.assertEqual(self.store.list_delegations(owner="alice"), [])
        with patch.dict(os.environ, {"KYREX_LEVEL6_SEND_ENABLED": "1"}):
            view = delegation.submit_delegation(
                "alice", self.chief, "calendar", "#L6Workout test",
                store=self.store)
            self.assertEqual(self.store.get(view["task_id"])["task_text"],
                             serve.LEVEL6_MESSAGE_TEST_REQUEST)


if __name__ == "__main__":
    unittest.main()
