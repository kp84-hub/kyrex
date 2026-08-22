"""Tier derivation tests for serve.py — executor contract compliance.

Per K_BOT_DESIGN.md, the host must derive an operation's tier from the
operation itself, not from what the executor claims. These tests verify that
derive_tier implements the rules correctly.

Run: python3 test_tier_derivation.py
"""
import os
import sys

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "12345")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve

failures = []


def check(name, cond, detail=""):
    if cond:
        print("  PASS  " + name)
    else:
        print("  FAIL  " + name + (" " + detail if detail else ""))
        failures.append(name)


print("\nTest 1: declared tier 1 with a benign summary stays 1")
result = serve.derive_tier("repo", {"tier": 1, "summary": "edit README"})
check("returns 1", result == 1, f"got {result}")


print("\nTest 2: declared tier 1 with a summary starting 'delete' becomes 2")
result = serve.derive_tier("repo", {"tier": 1, "summary": "delete the backup branch"})
check("returns 2", result == 2, f"got {result}")

result = serve.derive_tier("repo", {"tier": 1, "summary": "remove old config files"})
check("'remove' also escalates to 2", result == 2, f"got {result}")

result = serve.derive_tier("repo", {"tier": 1, "summary": "trash expired drafts"})
check("'trash' also escalates to 2", result == 2, f"got {result}")

result = serve.derive_tier("repo", {"tier": 1, "summary": "send the draft email"})
check("'send' also escalates to 2", result == 2, f"got {result}")

result = serve.derive_tier("repo", {"tier": 1, "summary": "push to production"})
check("'push' also escalates to 2", result == 2, f"got {result}")

result = serve.derive_tier("repo", {"tier": 1, "summary": "force sync remote"})
check("'force' also escalates to 2", result == 2, f"got {result}")

result = serve.derive_tier("repo", {"tier": 1, "summary": "revoke old API key"})
check("'revoke' also escalates to 2", result == 2, f"got {result}")

result = serve.derive_tier("repo", {"tier": 1, "summary": "drop the temp table"})
check("'drop' also escalates to 2", result == 2, f"got {result}")


print("\nTest 3: declared tier 2 stays 2")
result = serve.derive_tier("repo", {"tier": 2, "summary": "merge the PR"})
check("returns 2", result == 2, f"got {result}")


print("\nTest 4: declared tier 0 becomes 2")
result = serve.derive_tier("repo", {"tier": 0, "summary": "read config"})
check("returns 2", result == 2, f"got {result}")


print("\nTest 5: missing tier becomes 2")
result = serve.derive_tier("repo", {"summary": "no tier field"})
check("returns 2", result == 2, f"got {result}")


print("\nTest 6: declared tier 3 becomes 2")
result = serve.derive_tier("repo", {"tier": 3, "summary": "list files"})
check("returns 2", result == 2, f"got {result}")


print("\nTest 7: executor_prefix is accepted but not used for derivation yet")
result = serve.derive_tier("fs", {"tier": 1, "summary": "write notes.txt"})
check("fs executor tier 1 stays 1", result == 1, f"got {result}")


print("\nTest 8: empty summary does not crash")
result = serve.derive_tier("repo", {"tier": 1, "summary": ""})
check("empty summary stays 1", result == 1, f"got {result}")

result = serve.derive_tier("repo", {"tier": 1})
check("missing summary stays 1", result == 1, f"got {result}")


print("\n" + ("ALL TESTS PASSED" if not failures
              else "%d FAILURE(S): %s" % (len(failures), failures)))
sys.exit(1 if failures else 0)