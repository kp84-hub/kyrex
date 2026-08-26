"""Bot isolation — prove no global/cross-bot fallback in the cloud layer.

The shared-engine design (K_BOT_AUTONOMY.md) rests on one invariant: "No
Bot-specific state may have a global fallback." When two Bots share one
engine, resolving Bot A must yield A's policy and rift — and when A is
absent, corrupt, or unknown, it must resolve to the *unbound* default
(default-deny), NEVER silently to Bot B's state.

This exercises the real resolution chain: bots.get_bot / bots.load_bots,
serve.resolve_bot, serve.build_context, task_store._resolve_bot.

SCOPE: this covers Bot identity / policy / rift isolation in the cloud
layer. It does NOT cover the engine's ~/.px/config.json fallback (API keys /
model config) — that lives in the engine and needs its own test.

Run: python3 test_bot_isolation.py
"""
import importlib
import os
import sys
import tempfile

# Isolate the whole test in a throwaway data dir so we never touch a real
# bots.json. Must be set before bots/paths import and read DATA_DIR.
os.environ["KYREX_DATA_DIR"] = tempfile.mkdtemp(prefix="kyrex-iso-")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bots
import serve
import policy
import task_store

policy.MODE = "enforce"
failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "  " + detail))
    if not cond:
        failures.append(name)


def reset_registry():
    """Start from an empty registry and plant two distinct Bots."""
    for b in list(bots.load_bots()):
        bots.remove_bot(b)
    # alpha: may read+write fs, denied mail. rift under /tmp/alpha.
    bots.add_bot("alpha", "Alpha", "m", "/tmp/rift-alpha",
                 policy={"fs:read": 0, "fs:write": 1, "mail:send": "deny"})
    # beta: may send mail, denied fs. rift under /tmp/beta.
    bots.add_bot("beta", "Beta", "m", "/tmp/rift-beta",
                 policy={"mail:send": 1, "fs:write": "deny"})


reset_registry()

print("\n1. Each Bot resolves to its OWN policy and rift")
ca = serve.build_context("alpha")
cb = serve.build_context("beta")
check("alpha rift is alpha's", ca.rift_path == "/tmp/rift-alpha", ca.rift_path)
check("beta rift is beta's", cb.rift_path == "/tmp/rift-beta", cb.rift_path)
check("alpha policy is alpha's", ca.policy.get("fs:write") == 1 and "mail:send" in ca.policy)
check("beta policy is beta's", cb.policy.get("mail:send") == 1 and "fs:write" in cb.policy)
check("no rift bleed", ca.rift_path != cb.rift_path)

print("\n2. Policy does not leak across Bots (enforce mode)")
# alpha may write fs; beta must NOT (beta denies fs:write).
da = policy.enforce(policy.evaluate(ca.policy, "fs:write", serve.derive_host_tier("fs:write")))
db = policy.enforce(policy.evaluate(cb.policy, "fs:write", serve.derive_host_tier("fs:write")))
check("alpha fs:write allowed (its own rule)", da == 1, f"got {da}")
check("beta fs:write denied (not alpha's rule)", db == "deny", f"got {db}")
# beta may mail; alpha must NOT (alpha denies mail:send).
ma = policy.enforce(policy.evaluate(ca.policy, "mail:send", serve.derive_host_tier("mail:send")))
mb = policy.enforce(policy.evaluate(cb.policy, "mail:send", serve.derive_host_tier("mail:send")))
check("alpha mail:send denied (its own deny)", ma == "deny", f"got {ma}")
# mb is 2, not 1: mail:send is host-derived T2, and beta's policy rule (1)
# can only raise, never lower it. What isolation asserts is that beta can
# *reach* mail (not denied) while alpha cannot — not the exact tier.
check("beta mail:send reachable, not denied (its own rule)", mb != "deny", f"got {mb}")

print("\n3. Removing a Bot resolves to UNBOUND, never to the other Bot")
bots.remove_bot("alpha")
ca2 = serve.build_context("alpha")
check("removed alpha has no rift", ca2.rift_path is None, ca2.rift_path)
check("removed alpha did NOT inherit beta's rift", ca2.rift_path != "/tmp/rift-beta")
check("removed alpha gets unbound policy (safe reads only)",
      ca2.policy.get("fs:read") == 0 and "mail:send" not in ca2.policy)
# The dangerous case: alpha's now-denied mail must NOT become allowed by
# falling through to beta's mail rule.
m = policy.enforce(policy.evaluate(ca2.policy, "mail:send", serve.derive_host_tier("mail:send")))
check("removed alpha cannot mail via a fallback", m == "deny", f"got {m}")

print("\n4. Unknown Bot id: get_bot raises, resolvers return unbound/None")
try:
    bots.get_bot("ghost")
    check("get_bot('ghost') raises", False, "returned instead of raising")
except KeyError:
    check("get_bot('ghost') raises KeyError", True)
check("resolve_bot('ghost') is None", serve.resolve_bot("ghost") is None)
rb = task_store._resolve_bot("ghost")
check("_resolve_bot('ghost') carries no identity",
      rb["bot_id"] is None and rb["rift"] is None, str(rb))

print("\n5. Corrupt registry -> unbound default-deny, not another Bot's state")
bots_path = os.path.join(os.environ["KYREX_DATA_DIR"], "bots.json")
with open(bots_path, "w") as f:
    f.write("{ this is not valid json ")
cc = serve.build_context("beta")  # beta still 'exists' but file is corrupt
check("corrupt registry -> unbound (rift None)", cc.rift_path is None, cc.rift_path)
check("corrupt registry -> did NOT resolve beta's rift", cc.rift_path != "/tmp/rift-beta")
check("corrupt registry -> unbound policy, not beta's",
      cc.policy.get("fs:read") == 0 and cc.policy.get("mail:send") != 1)

print("\n6. Identity-chain caveat: unbound sessions share bot_id=executor_prefix")
# Not a policy leak (policy is UNBOUND for both, session_id differs), but the
# audit bot_id collides. Assert session_id stays distinct so the audit trail
# can still tell two unbound operators apart.
u1 = serve.build_context("operator-one", executor_prefix="repo")
u2 = serve.build_context("operator-two", executor_prefix="repo")
check("unbound bot_id collides (documented)", u1.bot_id == u2.bot_id == "repo")
check("but session_id stays distinct", u1.session_id != u2.session_id)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("ALL PASSED — no cross-bot fallback in the cloud resolution layer")
