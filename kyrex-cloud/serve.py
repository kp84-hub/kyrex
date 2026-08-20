#!/usr/bin/env python3
"""
serve.py — Core executor routing and repo alias resolution for Kyrex Cloud.

Constants and pure functions extracted from telegram_bot.py so they can be
imported by other modules (e.g., a future HTTP server) without pulling in
Telegram API dependencies.

This module has no Telegram imports and no dependency on telegram_bot.py.
"""
import json
import os
import re

# Executor routing — maps a message prefix to a script path relative to SCRIPT_DIR.
# The default executor handles messages with no recognized prefix.
EXECUTORS = {
    "repo": "git_workflow.py",
}
DEFAULT_EXECUTOR = "repo"

# Matches a single-word prefix at the very start of a message followed by ": ".
EXECUTOR_PREFIX_RE = re.compile(r"^(\w+):\s+(.*)")

try:
    REPO_ALIASES = json.loads(os.environ.get("KYREX_REPO_ALIASES", "{}"))
except json.JSONDecodeError:
    REPO_ALIASES = {}


def resolve_executor(text: str):
    """Parse a leading '<prefix>: ' from task text for executor routing.

    Returns (executor_prefix, task_text, error_word) where:
      - executor_prefix is a key in EXECUTORS, or DEFAULT_EXECUTOR on no match
      - task_text is the text with prefix stripped (or whole text on no match)
      - error_word is None unless an unknown prefix was detected, in which
        case it holds the unknown word and executor_prefix is None

    Known executor prefixes (EXECUTORS) are routed to their script.
    Repo aliases (REPO_ALIASES) are NOT consumed here — they fall through to
    DEFAULT_EXECUTOR so the alias prefix is preserved for resolve_repo inside
    the repo executor's command builder.
    Unknown prefixes that aren't aliases either are rejected.
    Text with no prefix match at all routes to DEFAULT_EXECUTOR."""
    m = EXECUTOR_PREFIX_RE.match(text)
    if m:
        prefix = m.group(1).lower()
        rest = m.group(2)
        if prefix in EXECUTORS:
            return prefix, rest, None
        # Repo aliases pass through to default executor with full text intact
        # so resolve_repo can strip the alias inside build_command.
        if prefix in REPO_ALIASES:
            return DEFAULT_EXECUTOR, text, None
        return None, None, prefix  # unknown prefix → rejection
    return DEFAULT_EXECUTOR, text, None