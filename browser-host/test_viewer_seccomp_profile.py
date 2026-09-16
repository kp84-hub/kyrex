"""Semantic regressions for the repository-managed viewer seccomp profile.

VERIFIED production outcome this suite locks in:

  * Docker's builtin seccomp profile is the real blocker for Chromium's
    USER-NAMESPACE sandbox; **AppArmor is not**. It stays ``docker-default``
    enforcing and needs no customisation.
  * ``seccomp=unconfined`` while ``docker-default`` AppArmor stayed enforcing
    made Chromium succeed (rendered ``about:blank``, rc=0) — proof the gate is
    seccomp, not AppArmor.
  * The VPS syscall trace of that successful run required exactly four calls,
    and NO mount / pivot_root / setns:
        clone(CLONE_NEWUSER|SIGCHLD)
        clone(CLONE_NEWUSER|CLONE_NEWPID|CLONE_NEWNET|SIGCHLD)
        clone(CLONE_NEWPID|SIGCHLD)
        unshare(CLONE_NEWUSER)
  * ``clone3`` keeps Docker's default ENOSYS (errno 38) fallback.

These tests PARSE the seccomp JSON semantically (never substring-match the
policy) and prove the default-deny posture is Docker/Moby's, widened by ONLY
those four rules, on amd64, for the viewer service alone.

Run: python3 -m pytest browser-host/test_viewer_seccomp_profile.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

BROWSER_HOST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import viewer_ctl as vc  # noqa: E402

SECCOMP_PROFILE = BROWSER_HOST_DIR / "seccomp" / "kyrex-viewer-chromium.json"
SECCOMP_README = BROWSER_HOST_DIR / "seccomp" / "README.md"
COMPOSE_VIEWER = BROWSER_HOST_DIR / "docker-compose.viewer.yml"
DOCKERFILE_VIEWER = BROWSER_HOST_DIR / "Dockerfile.viewer"

# ── recorded provenance of the base allowlist ─────────────────────────
UPSTREAM_REPO = "moby/profiles"
UPSTREAM_COMMIT = "65adc7e022c97f55e45c054ff012988027733b87"
UPSTREAM_SHA256 = "785b2429264afba4d594320337cb17f144f3c7d51585f9805eef72e28f4f9334"

SECCOMP_ENTRY = "seccomp=./seccomp/kyrex-viewer-chromium.json"

# ── the four exact rules the trace justifies (amd64 only) ─────────────
CLONE_NEWUSER = 0x10000000
CLONE_NEWPID = 0x20000000
CLONE_NEWNET = 0x40000000
SIGCHLD = 0x11

EXPECTED_NAMESPACE_RULES = [
    {"names": ["clone"], "action": "SCMP_ACT_ALLOW",
     "args": [{"index": 0, "value": CLONE_NEWUSER | SIGCHLD, "op": "SCMP_CMP_EQ"}],
     "includes": {"arches": ["amd64"]}},
    {"names": ["clone"], "action": "SCMP_ACT_ALLOW",
     "args": [{"index": 0,
               "value": CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNET | SIGCHLD,
               "op": "SCMP_CMP_EQ"}],
     "includes": {"arches": ["amd64"]}},
    {"names": ["clone"], "action": "SCMP_ACT_ALLOW",
     "args": [{"index": 0, "value": CLONE_NEWPID | SIGCHLD, "op": "SCMP_CMP_EQ"}],
     "includes": {"arches": ["amd64"]}},
    {"names": ["unshare"], "action": "SCMP_ACT_ALLOW",
     "args": [{"index": 0, "value": CLONE_NEWUSER, "op": "SCMP_CMP_EQ"}],
     "includes": {"arches": ["amd64"]}},
]

# Docker's OWN default clone rule — must be left untouched.
UPSTREAM_CLONE_MASKED_RULE = {
    "names": ["clone"], "action": "SCMP_ACT_ALLOW",
    "args": [{"index": 0, "value": 2114060288, "op": "SCMP_CMP_MASKED_EQ"}],
    "excludes": {"caps": ["CAP_SYS_ADMIN"], "arches": ["s390", "s390x"]},
}

# Docker's own default ptrace rule: allowed uncapped for kernels >= 4.8. It is
# part of the Docker posture we PRESERVE, so it must remain byte-identical —
# it is emphatically NOT something this profile newly adds.
UPSTREAM_PTRACE_RULE = {
    "names": ["process_vm_readv", "process_vm_writev", "ptrace"],
    "action": "SCMP_ACT_ALLOW",
    "includes": {"minKernel": "4.8"},
}

# Syscalls that must NEVER become reachable without a capability gate. (ptrace
# is deliberately absent — see UPSTREAM_PTRACE_RULE.)
DANGEROUS = (
    "mount", "pivot_root", "setns", "bpf", "perf_event_open",
    "keyctl", "add_key", "request_key", "clone3",
    "kexec_load", "init_module", "finit_module", "delete_module",
    "open_by_handle_at", "swapon", "swapoff", "umount", "umount2",
    "reboot", "iopl", "ioperm",
)

# Base allowlist spot-checks: the normal Docker posture must survive.
BASE_ALLOWLIST = (
    "read", "write", "openat", "mmap", "execve", "futex", "prctl",
    "seccomp", "socket", "wait4", "pipe2", "epoll_wait", "clone", "unshare",
)


# ── helpers ───────────────────────────────────────────────────────────

def _profile() -> dict:
    return json.loads(SECCOMP_PROFILE.read_text())


def _rules(profile: dict) -> list:
    return profile["syscalls"]


def _names(rule: dict) -> set:
    return set(rule.get("names") or [])


def _arch_ok_for_amd64(rule: dict) -> bool:
    inc = ((rule.get("includes") or {}).get("arches")) or []
    exc = ((rule.get("excludes") or {}).get("arches")) or []
    if inc and "amd64" not in inc:
        return False
    return "amd64" not in exc


def _is_cap_gated(rule: dict) -> bool:
    """True iff the ALLOW only takes effect WITH a cap (inactive here)."""
    return bool(((rule.get("includes") or {}).get("caps")))


def _uncapped_allows(profile: dict) -> list:
    """ALLOW rules that are ACTIVE for our caps-free amd64 container."""
    out = []
    for r in _rules(profile):
        if r.get("action") != "SCMP_ACT_ALLOW":
            continue
        if not _arch_ok_for_amd64(r):
            continue
        if _is_cap_gated(r):
            continue
        out.append(r)
    return out


def _uncapped_names(profile: dict) -> set:
    names: set = set()
    for r in _uncapped_allows(profile):
        names |= _names(r)
    return names


def _canon(rule: dict) -> str:
    return json.dumps(rule, sort_keys=True)


# ── 1. default action is default-deny, never global ALLOW ─────────────

def test_default_action_is_errno_not_allow():
    p = _profile()
    assert p["defaultAction"] == "SCMP_ACT_ERRNO"
    assert p["defaultAction"] != "SCMP_ACT_ALLOW"
    assert p.get("defaultErrnoRet") == 1


def test_default_allow_fixture_is_rejected_by_the_detector():
    bad = {"defaultAction": "SCMP_ACT_ALLOW", "syscalls": EXPECTED_NAMESPACE_RULES}
    assert bad["defaultAction"] == "SCMP_ACT_ALLOW"
    assert not (bad.get("defaultAction") == "SCMP_ACT_ERRNO")
    assert _profile().get("defaultAction") == "SCMP_ACT_ERRNO"


# ── 2. only the four exact rules widen namespaces ─────────────────────

def test_only_the_four_narrow_rules_widen_namespaces():
    uncapped_ns = [r for r in _uncapped_allows(_profile())
                   if _names(r) & {"clone", "unshare"}]
    expected = EXPECTED_NAMESPACE_RULES + [UPSTREAM_CLONE_MASKED_RULE]
    assert sorted(map(_canon, uncapped_ns)) == sorted(map(_canon, expected))


def test_namespace_rules_use_exact_eq_values_on_amd64():
    rules = _rules(_profile())
    for expected in EXPECTED_NAMESPACE_RULES:
        assert expected in rules, expected
        assert expected["includes"]["arches"] == ["amd64"]
        for arg in expected["args"]:
            # exact equality — NOT a mask, so no adjacent flag is admitted
            assert arg["op"] == "SCMP_CMP_EQ"


def test_no_namespace_rule_is_mask_matched_except_dockers_own():
    # Only UNCAPPED allows matter: the CAP_SYS_ADMIN-gated rule is inert here.
    for r in _uncapped_allows(_profile()):
        if not (_names(r) & {"clone", "unshare"}):
            continue
        if r in EXPECTED_NAMESPACE_RULES:
            continue
        # the ONLY other active namespace allow is Docker's own masked rule
        assert r == UPSTREAM_CLONE_MASKED_RULE, r


# ── 3. arbitrary clone/unshare stay denied ────────────────────────────

def test_arbitrary_clone_and_unshare_remain_denied():
    uncapped_ns = [r for r in _uncapped_allows(_profile())
                   if _names(r) & {"clone", "unshare"}]
    unshare_uncapped = [r for r in uncapped_ns if "unshare" in _names(r)]
    assert unshare_uncapped == [EXPECTED_NAMESPACE_RULES[3]], (
        "unshare must be allowed for CLONE_NEWUSER ONLY — no arbitrary unshare")
    for r in (x for x in uncapped_ns if "clone" in _names(x)):
        assert r in EXPECTED_NAMESPACE_RULES or r == UPSTREAM_CLONE_MASKED_RULE, r


# ── 4. the dangerous set is never newly allowed ───────────────────────

def test_dangerous_syscalls_are_not_newly_allowed():
    allowed = _uncapped_names(_profile())
    for name in DANGEROUS:
        assert name not in allowed, (
            f"{name} must not be reachable without a capability gate")


def test_ptrace_keeps_docker_default_and_is_not_newly_widened():
    uncapped_ptrace = [r for r in _uncapped_allows(_profile())
                       if "ptrace" in _names(r)]
    assert uncapped_ptrace == [UPSTREAM_PTRACE_RULE], uncapped_ptrace


def test_pivot_root_is_allowed_nowhere():
    assert not any(
        r.get("action") == "SCMP_ACT_ALLOW" and "pivot_root" in _names(r)
        for r in _rules(_profile()))


def test_capability_gated_denials_are_retained():
    mountish = {"mount", "setns", "umount", "umount2", "bpf", "perf_event_open"}
    for r in _rules(_profile()):
        if r.get("action") == "SCMP_ACT_ALLOW" and (_names(r) & mountish):
            assert _is_cap_gated(r), f"{r} must be CAP_SYS_ADMIN-gated"


# ── 5. clone3 keeps Docker's ENOSYS fallback ──────────────────────────

def test_clone3_keeps_docker_default_enosys_fallback():
    rules = [r for r in _rules(_profile()) if "clone3" in _names(r)]
    errno_rules = [r for r in rules if r.get("action") == "SCMP_ACT_ERRNO"]
    assert len(errno_rules) == 1, rules
    r = errno_rules[0]
    assert r["errnoRet"] == 38
    assert r["excludes"]["caps"] == ["CAP_SYS_ADMIN"]
    # no UNCAPPED rule may allow clone3 (the other mention is the
    # CAP_SYS_ADMIN-gated allow, which is inert for this container)
    assert not any(x.get("action") == "SCMP_ACT_ALLOW" and not _is_cap_gated(x)
                   for x in rules)


# ── 6. the normal Docker allow posture survives ───────────────────────

def test_docker_default_allowlist_is_preserved():
    allowed = _uncapped_names(_profile())
    for name in BASE_ALLOWLIST:
        assert name in allowed, f"base allowlist entry lost: {name}"


# ── 7. compose binds the profile to the viewer service ONLY ───────────

def _compose() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(COMPOSE_VIEWER.read_text())


def test_compose_binds_profile_to_viewer_service_only():
    doc = _compose()
    services = doc["services"]
    assert list(services) == ["viewer"], list(services)
    assert services["viewer"].get("security_opt") == [SECCOMP_ENTRY]
    rel = SECCOMP_ENTRY.split("=", 1)[1]
    assert (COMPOSE_VIEWER.parent / rel).resolve() == SECCOMP_PROFILE.resolve()
    for other in ("docker-compose.yml", "docker-compose.cloud.yml"):
        text = (BROWSER_HOST_DIR / other).read_text()
        assert "security_opt" not in text, f"{other} must stay on defaults"


# ── 8. AppArmor stays docker-default / enforcing ──────────────────────

def test_apparmor_is_not_overridden_and_no_profile_is_added():
    doc = _compose()
    for name, svc in doc["services"].items():
        for opt in svc.get("security_opt", []):
            assert not opt.startswith("apparmor="), (
                f"{name} must not override AppArmor: {opt}")
    assert not (BROWSER_HOST_DIR / "apparmor").exists(), (
        "no AppArmor profile is needed — docker-default stays enforcing")


# ── 9. no privileges / bypass / global mutation ───────────────────────

def test_no_privileges_or_capabilities_in_compose():
    doc = _compose()
    for name, svc in doc["services"].items():
        for banned in ("privileged", "cap_add", "cap_drop", "devices",
                       "pid", "userns_mode"):
            assert banned not in svc, f"{name} must not set `{banned}`"


def test_no_bypass_tokens_or_sys_admin_anywhere():
    compose_text = COMPOSE_VIEWER.read_text()
    for tok in ("seccomp=unconfined", "apparmor=unconfined", "--no-sandbox"):
        assert tok not in compose_text, tok
        assert tok not in SECCOMP_PROFILE.read_text(), tok
        assert tok not in DOCKERFILE_VIEWER.read_text(), tok
    assert "SYS_ADMIN" not in compose_text
    # And the profile file itself is never the global-ALLOW posture.
    assert _profile()["defaultAction"] != "SCMP_ACT_ALLOW"


def test_no_global_sysctl_or_daemon_change_is_encoded():
    offenders = []
    me = Path(__file__).name
    needles = ("sysctl -w", "/etc/sysctl", "/etc/docker/daemon.json")
    for p in BROWSER_HOST_DIR.rglob("*"):
        if not p.is_file() or p.name == me:
            continue
        if p.suffix not in {".py", ".yml", ".yaml", ".sh", ".json", ".md"}:
            continue
        text = p.read_text(errors="ignore")
        if any(n in text for n in needles):
            offenders.append(str(p.relative_to(BROWSER_HOST_DIR)))
    assert not offenders, offenders


# ── 10. the viewer intentionally selects the userns sandbox ───────────

def test_viewer_ctl_selects_the_userns_sandbox():
    argv = vc.build_chromium_args("/profiles/bot-b1/owner-me")
    assert "--disable-setuid-sandbox" in argv
    assert "--no-sandbox" not in argv
    assert not any("no-sandbox" in a for a in argv)


# ── 11. provenance is recorded ────────────────────────────────────────

def test_provenance_is_recorded():
    assert SECCOMP_README.is_file(), "seccomp README (provenance) is required"
    text = SECCOMP_README.read_text()
    for needle in (UPSTREAM_REPO, UPSTREAM_COMMIT, UPSTREAM_SHA256):
        assert needle in text, needle
