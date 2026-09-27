"""Focused safety regressions for the fixed #L6Workout message operation."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import browser_operator as bo
import browser_host_channel as channel
import browser_hosts as hosts
import level6_weekly
import serve


def message():
    lines = ["#L6Workout", "", "🏋️ Level 6 — Workout Week"]
    for day in range(21, 27):
        lines.extend([f"Monday 2026-09-{day} — Workout {day}",
                      f"Trainer: Coach {day}"])
    return "\n".join(lines)


def spec(text=None):
    return json.dumps({
        bo.GOOGLE_MESSAGES_LEVEL6_ACTION: True,
        "url": bo.GOOGLE_MESSAGES_BASE_URL,
        "message": text or message(),
    })


class Protocol:
    def __init__(self, allow=True):
        self.allow = allow
        self.calls = []

    def operation(self, op, target, summary, detail):
        self.calls.append((op, target))
        return self.allow

    def progress(self, _note):
        pass

    def redact(self, value):
        return str(value)


class Driver:
    def __init__(self, root):
        self.session_dir = Path(root)


class GoogleMessagesLevel6Tests(unittest.TestCase):
    def test_exact_spec_and_policy(self):
        action = bo.parse_spec(spec())[0]
        self.assertEqual(action["action"], bo.GOOGLE_MESSAGES_LEVEL6_ACTION)
        self.assertEqual(bo.action_operation(action), "messages.send_level6")
        self.assertTrue(bo.preflight(spec(), ["messages.google.com"])[0])
        self.assertFalse(bo.preflight(spec(), ["example.com"])[0])
        self.assertEqual(serve.OPERATION_TIERS["messages:send_level6"], 0)
        self.assertEqual(serve.CALENDAR_PRESET["messages:send_level6"], 0)
        self.assertTrue(serve.is_calendar_bot_policy(serve.CALENDAR_PRESET))

    def test_payload_is_bounded_and_fixed_shape(self):
        with self.assertRaises(bo.SpecError):
            bo.parse_spec(spec("hello"))
        with self.assertRaises(bo.SpecError):
            bo.parse_spec(json.dumps({
                bo.GOOGLE_MESSAGES_LEVEL6_ACTION: True,
                "url": "https://example.com/", "message": message()}))

    def test_missing_host_destination_fails_before_browser_use(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            result = bo._run_google_messages_level6(
                Driver(root), Protocol(), bo.parse_spec(spec())[0],
                root=Path(root), allowlist=["messages.google.com"])
        self.assertEqual(result["status"], "error")

    def test_duplicate_payload_is_not_sent_again(self):
        with tempfile.TemporaryDirectory() as root:
            digest = hashlib.sha256(message().encode()).hexdigest()
            Path(root, ".kyrex-level6-message-receipts.json").write_text(
                json.dumps([digest]), encoding="utf-8")
            with patch.dict(os.environ, {
                "KYREX_GOOGLE_MESSAGES_CONVERSATION_URL":
                "https://messages.google.com/web/conversations/test"
            }, clear=True):
                proto = Protocol()
                result = bo._run_google_messages_level6(
                    Driver(root), proto, bo.parse_spec(spec())[0],
                    root=Path(root), allowlist=["messages.google.com"])
        self.assertEqual(result["status"], "no_changes")
        self.assertEqual(proto.calls, [])

    def test_paired_messages_profile_uses_calendar_bot_host_binding(self):
        ctx = SimpleNamespace(bot_owner="alice", bot_id="calendar",
                              policy=serve.calendar_preset_policy(),
                              browser_allowlist=[])
        selected = []
        host = SimpleNamespace(host_id="host-1")
        fake_channel = SimpleNamespace(
            authenticated=True,
            dispatch_task=lambda **kw: selected.append(kw) or {"status": "ok"})
        manager = channel.HostManager()
        with (patch.object(hosts, "binding_for", side_effect=lambda owner, bot: (
                "host-1" if (owner, bot) == ("alice", "calendar") else "")),
              patch.object(hosts, "host_for", side_effect=lambda owner, bot: (
                  selected.append((owner, bot)) or host)),
              patch.object(serve, "build_context", return_value=ctx),
              patch.object(manager, "channel_for", return_value=fake_channel),
              patch("browser_host_bridge.request_browser_dispatch",
                    return_value=({"status": "ok"}, None)) as dispatch):
            result, error = serve.browser_host_dispatch(
                ctx, spec(), profile_bot_id=serve.LEVEL6_MESSAGES_PROFILE_ID)
            self.assertIsNone(error)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(dispatch.call_args.kwargs["host_id"], "host-1")
            self.assertEqual(manager.dispatch_browser_task(
                "alice", "calendar", spec(),
                profile_bot_id=serve.LEVEL6_MESSAGES_PROFILE_ID),
                {"status": "ok"})
        self.assertIn(("alice", "calendar"), selected)
        self.assertEqual(selected[-1]["bot_id"], serve.LEVEL6_MESSAGES_PROFILE_ID)

    def test_messages_profile_rejects_generic_browser_tasks(self):
        ctx = SimpleNamespace(bot_owner="alice", bot_id="calendar",
                              policy=serve.calendar_preset_policy(),
                              browser_allowlist=[])
        with patch.object(serve, "build_context", return_value=ctx):
            with self.assertRaises(channel.ChannelError):
                channel.HostManager().dispatch_browser_task(
                    "alice", "calendar", '{"url":"https://messages.google.com/web/"}',
                    profile_bot_id=serve.LEVEL6_MESSAGES_PROFILE_ID)

    def test_send_task_reads_facebook_week_before_group_send(self):
        calendar_ctx = SimpleNamespace(bot_owner="alice", bot_id="calendar",
                                       policy=serve.calendar_preset_policy())
        weekly_ctx = SimpleNamespace(bot_owner="alice", bot_id="weekly",
                                     policy=serve.level6_weekly_preset_policy())
        weekly_bot = {"id": "weekly", "owner": "alice", "status": "running",
                      "policy": serve.level6_weekly_preset_policy()}
        lines = [f"Monday 2026-09-{d} — Workout {d} — trainer: Coach"
                 for d in range(21, 27)]
        results, messages = [], []
        with (patch("bots.load_bots", return_value={"weekly": weekly_bot}),
              patch.object(hosts, "binding_for", return_value="host-1"),
              patch.object(serve, "build_context", return_value=weekly_ctx),
              patch.object(level6_weekly, "run_weekly", return_value=lines) as read,
              patch.object(serve, "browser_host_dispatch",
                           return_value=({"status": "ok", "final_response": "Sent"}, None)) as send):
            serve._run_level6_facebook_message_task(
                calendar_ctx, "alice", serve.LEVEL6_MESSAGE_REQUEST,
                lambda owner, text: messages.append(text),
                on_result=results.append)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs["profile_bot_id"], "google-messages")
        outgoing = json.loads(send.call_args.args[1])
        self.assertIn("Workout 21", outgoing["message"])
        self.assertEqual(results[0]["count"], 6)
        self.assertEqual(messages, ["Sent"])

    def test_facebook_login_failure_never_sends(self):
        calendar_ctx = SimpleNamespace(bot_owner="alice", bot_id="calendar",
                                       policy=serve.calendar_preset_policy())
        weekly_ctx = SimpleNamespace(bot_owner="alice", bot_id="weekly",
                                     policy=serve.level6_weekly_preset_policy())
        weekly_bot = {"id": "weekly", "owner": "alice", "status": "running",
                      "policy": serve.level6_weekly_preset_policy()}
        messages = []
        with (patch("bots.load_bots", return_value={"weekly": weekly_bot}),
              patch.object(hosts, "binding_for", return_value="host-1"),
              patch.object(serve, "build_context", return_value=weekly_ctx),
              patch.object(level6_weekly, "run_weekly",
                           side_effect=level6_weekly.Level6Error("Facebook login expired")),
              patch.object(serve, "browser_host_dispatch") as send):
            serve._run_level6_facebook_message_task(
                calendar_ctx, "alice", serve.LEVEL6_MESSAGE_REQUEST,
                lambda owner, text: messages.append(text))
        send.assert_not_called()
        self.assertIn("Facebook login expired", messages[0])


if __name__ == "__main__":
    unittest.main()
