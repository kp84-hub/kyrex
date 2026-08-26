"""Governance tests — host-derived tiers, escalation, unbound policy.

Enforces K_BOT_DESIGN.md / K_BOT_AUTONOMY.md. Replaces the role of
test_tier_derivation.py, whose cases pinned the old executor-trusting
behaviour. Run: python3 test_tier_governance.py
"""
import os
import sys

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "12345")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serve
import policy

failures = []


def check(name, cond, detail=""):
    if cond:
        print("  PASS  " + name)
    else:
        print("  FAIL  " + name + (" " + detail if detail else ""))
        failures.append(name)


print("\n1. Host derives base tier from the operation table")
check("fs:read is T0", serve.derive_host_tier("fs:read") == 0)
check("cal:list is T0", serve.derive_host_tier("cal:list") == 0)
check("fs:write is T1", serve.derive_host_tier("fs:write") == 1)
check("fs:delete is T2", serve.derive_host_tier("fs:delete") == 2)
check("mail:send is T2", serve.derive_host_tier("mail:send") == 2)
check("unknown op -> None", serve.derive_host_tier("mail:nuke") is None)

print("\n2. Scope escalation forces T2 for self-directed targets")
check("write under kyrex-cloud/ -> T2",
      serve.derive_host_tier("fs:write", "kyrex-cloud/serve.py") == 2)
check("write under ~/.kyrex/ -> T2",
      serve.derive_host_tier("fs:write", "/home/k/.kyrex/registry.json") == 2)
check("read under kyrex-cloud/ still T2 (self-scope beats base)",
      serve.derive_host_tier("fs:read", "kyrex-cloud/policy.py") == 2)
check("ordinary path unaffected",
      serve.derive_host_tier("fs:write", "~/projects/foo/a.py") == 1)

print("\n3. Volume escalation on a structured count")
check("51 targets -> T2", serve.derive_host_tier("fs:write", count=51) == 2)
check("50 targets stays T1", serve.derive_host_tier("fs:write", count=50) == 1)
check("no count -> unchanged", serve.derive_host_tier("fs:write") == 1)

print("\n4. Declared tier is a hint that can only RAISE")
check("declared 2 raises a T0 read to T2",
      serve.derive_host_tier("fs:read", declared=2) == 2)
check("declared 0 cannot lower a T1 write",
      serve.derive_host_tier("fs:write", declared=0) == 1)
check("declared None ignored", serve.derive_host_tier("fs:delete", declared=None) == 2)

print("\n5. Approval path: ambiguity escalates, never T0")
check("benign unknown op -> T1",
      serve.derive_tier("repo", {"summary": "edit README"}) == 1)
check("destructive verb -> T2",
      serve.derive_tier("repo", {"summary": "delete the backup branch"}) == 2)
check("structured op wins over summary",
      serve.derive_tier("fs", {"op": "fs.read", "summary": "delete everything"}) == 0)
check("declared can only raise on approval path",
      serve.derive_tier("repo", {"summary": "edit README", "tier": 2}) == 2)
check("scope-sensitive approval -> T2",
      serve.derive_tier("fs", {"op": "fs.write", "target": "kyrex-cloud/x.py"}) == 2)

print("\n6. Unbound session carries an explicit read-only policy (not empty)")
ctx = serve.build_context("no-such-bot", executor_prefix="fs")
check("unbound rift is None", ctx.rift_path is None)
check("unbound policy grants fs:read", ctx.policy.get("fs:read") == 0)
check("unbound policy grants cal:list", ctx.policy.get("cal:list") == 0)
check("unbound policy does NOT grant fs:write", "fs:write" not in ctx.policy)

print("\n7. Default-deny preserved for unbound mutations; reads allowed")
policy.MODE = "enforce"
# cal:list: policy grants 0, host derives 0 -> effective 0 (ALLOW)
d = policy.evaluate(ctx.policy, "cal:list", serve.derive_host_tier("cal:list"))
check("unbound cal:list -> tier 0 (ALLOW)", policy.enforce(d) == 0, f"got {policy.enforce(d)}")
# fs:delete: no rule in unbound policy -> deny
d = policy.evaluate(ctx.policy, "fs:delete", serve.derive_host_tier("fs:delete"))
check("unbound fs:delete -> deny", policy.enforce(d) == "deny", f"got {policy.enforce(d)}")
# fs:read granted
d = policy.evaluate(ctx.policy, "fs:read", serve.derive_host_tier("fs:read"))
check("unbound fs:read -> tier 0 (ALLOW)", policy.enforce(d) == 0, f"got {policy.enforce(d)}")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("ALL PASSED")
