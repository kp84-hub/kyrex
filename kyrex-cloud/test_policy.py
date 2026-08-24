"""Tests for policy.py — Bot policy evaluation engine.

Covers: exact match beats prefix wildcard beats *, * catches all,
no matching rule denies, deny rule denies over broader allow, policy
cannot lower tier below derived, dry_run vs enforce mode.

Run: python3 test_policy.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import policy

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# ── Test 1: exact rule beats prefix wildcard ───────────────────────────
print("Test 1: exact rule beats prefix wildcard")
pol = {
    "cal:list": 0,
    "fs:read": 0,
    "fs:*": 2,
}
d = policy.evaluate(pol, "fs:read", derived_tier=1)
check("exact match fires when exact key exists",
      d["effective_tier"] == 1, f"got {d['effective_tier']!r}")
check("matched_rule is the exact key",
      d["matched_rule"] == "fs:read", f"got {d['matched_rule']!r}")


# ── Test 2: prefix wildcard matches when exact is absent ───────────────
print("\nTest 2: prefix wildcard matches when exact is absent")
pol = {
    "fs:*": 0,
}
d = policy.evaluate(pol, "fs:write", derived_tier=2)
check("prefix wildcard fires",
      d["effective_tier"] == 2, f"got {d['effective_tier']!r}")
check("matched_rule is the wildcard key",
      d["matched_rule"] == "fs:*", f"got {d['matched_rule']!r}")


# ── Test 3: * catches all when nothing more specific exists ────────────
print("\nTest 3: * catches all when nothing more specific exists")
pol = {
    "*": 1,
}
d = policy.evaluate(pol, "db:query", derived_tier=0)
check("catch-all fires",
      d["effective_tier"] == 1, f"got {d['effective_tier']!r}")
check("matched_rule is *",
      d["matched_rule"] == "*", f"got {d['matched_rule']!r}")


# ── Test 4: no matching rule denies ────────────────────────────────────
print("\nTest 4: no matching rule denies")
pol = {
    "fs:read": 0,
}
d = policy.evaluate(pol, "db:query", derived_tier=1)
check("no match yields deny",
      d["effective_tier"] == "deny", f"got {d['effective_tier']!r}")
check("matched_rule is None",
      d["matched_rule"] is None, f"got {d['matched_rule']!r}")


# ── Test 5: deny rule denies even when a broader rule allows ──────────
print("\nTest 5: deny rule denies even when a broader rule allows")
pol = {
    "db:*": 0,
    "db:drop": "deny",
}
d = policy.evaluate(pol, "db:drop", derived_tier=0)
check("deny rule fires",
      d["effective_tier"] == "deny", f"got {d['effective_tier']!r}")
check("matched_rule is the deny key",
      d["matched_rule"] == "db:drop", f"got {d['matched_rule']!r}")


# ── Test 6: policy cannot lower tier below derived tier ────────────────
print("\nTest 6: policy cannot lower tier below derived tier")
pol = {
    "fs:read": 0,
}
d = policy.evaluate(pol, "fs:read", derived_tier=2)
check("effective tier is max(policy, derived)=max(0,2)=2",
      d["effective_tier"] == 2, f"got {d['effective_tier']!r}")

# Also verify that policy *can* make the effective tier *higher* than
# the derived tier.
pol2 = {
    "fs:read": 2,
}
d2 = policy.evaluate(pol2, "fs:read", derived_tier=0)
check("policy can raise tier above derived: max(2,0)=2",
      d2["effective_tier"] == 2, f"got {d2['effective_tier']!r}")


# ── Test 7: dry_run mode leaves derived tier untouched ─────────────────
print("\nTest 7: dry_run mode leaves derived tier untouched")
policy.MODE = "dry_run"
pol = {
    "fs:read": 0,
}
d = policy.evaluate(pol, "fs:read", derived_tier=2)
result = policy.enforce(d)
check("dry_run returns derived_tier (2), not effective_tier",
      result == 2, f"got {result!r}")


# ── Test 8: enforce mode applies the effective tier ────────────────────
print("\nTest 8: enforce mode applies the effective tier")
policy.MODE = "enforce"
pol = {
    "fs:read": 0,
}
d = policy.evaluate(pol, "fs:read", derived_tier=2)
result = policy.enforce(d)
check("enforce returns effective_tier (2)",
      result == 2, f"got {result!r}")

# With a policy that grants lower than derived, enforce still returns max.
pol2 = {
    "fs:read": 0,
}
d2 = policy.evaluate(pol2, "fs:read", derived_tier=1)
result2 = policy.enforce(d2)
check("enforce returns max(policy,derived)=1",
      result2 == 1, f"got {result2!r}")

# With deny in enforce mode, enforce returns "deny".
pol3 = {
    "fs:read": "deny",
}
d3 = policy.evaluate(pol3, "fs:read", derived_tier=0)
result3 = policy.enforce(d3)
check("enforce returns 'deny' when rule denies",
      result3 == "deny", f"got {result3!r}")


# ── Summary ────────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)