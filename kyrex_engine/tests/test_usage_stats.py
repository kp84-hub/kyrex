"""Test get_usage_stats() and the /usage command emission.

Builds a minimal PlaneExecute instance (bypassing the heavy constructor that
builds the workspace file tree, discovers skills, and starts MCP servers) so
the tests exercise the real get_usage_stats()/handle_command() logic against
known state without network or filesystem side effects.
"""

import io
import json
import sys
from types import SimpleNamespace

import pytest

from kyrex.core import PlaneExecute


def _make_engine():
    """Minimal PlaneExecute instance with known usage state."""
    engine = object.__new__(PlaneExecute)
    engine.session = SimpleNamespace(
        history=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
    )
    engine._total_prompt_tokens = 100
    engine._total_completion_tokens = 50
    engine._compaction_count = 1
    engine._last_compaction_before = 10
    engine._last_compaction_after = 5
    engine.context_limit = 128000
    engine.model = "test-model"
    engine.provider = SimpleNamespace(name="test-provider")
    return engine


@pytest.fixture
def engine():
    return _make_engine()


def _capture_usage_output(engine):
    """Run handle_command('/usage') and return the parsed stdout JSON."""
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        engine.handle_command("/usage")
        sys.stdout.seek(0)
        output = sys.stdout.read()
    finally:
        sys.stdout = old_stdout
    return json.loads(output.strip())


class TestGetUsageStats:
    """Test get_usage_stats() reflects known engine state."""

    def test_reports_prompt_tokens(self, engine):
        assert engine.get_usage_stats()["prompt_tokens"] == 100

    def test_reports_completion_tokens(self, engine):
        assert engine.get_usage_stats()["completion_tokens"] == 50

    def test_reports_history_message_count(self, engine):
        assert engine.get_usage_stats()["history_messages"] == 2


class TestUsageCommand:
    """Test handle_command('/usage') emits valid JSON matching get_usage_stats."""

    def test_emits_valid_json_with_expected_keys(self, engine):
        msg = _capture_usage_output(engine)
        assert msg["type"] == "tui_pause"
        assert msg["value"] == "usage_stats"
        assert "files" in msg

    def test_emitted_files_match_get_usage_stats(self, engine):
        expected = engine.get_usage_stats()
        msg = _capture_usage_output(engine)
        assert msg["files"] == expected
