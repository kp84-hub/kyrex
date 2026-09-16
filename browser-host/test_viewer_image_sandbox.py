"""Regressions for the REAL-VPS viewer blockers: Chromium sandbox + X11 dir.

The manual-viewer smoke test on the VPS (image ``kyrex-browser-host:viewer``)
failed closed with two runtime blockers, both rooted in the IMAGE:

  1. Chromium exited ``No usable sandbox!`` — the image shipped ``chromium``
     but NOT ``chromium-sandbox``. The ``chromium`` package only *Recommends*
     the setuid sandbox helper, and the image installs with
     ``--no-install-recommends``, so the helper was silently omitted. On a host
     whose AppArmor sysctl ``kernel.apparmor_restrict_unprivileged_userns=1``
     blocks the unprivileged user-namespace sandbox, Chromium then has no
     usable sandbox at all.
  2. Xvfb logged ``_XSERVTransmkdir: ERROR: euid != 0, directory /tmp/.X11-unix
     will not be created`` — the non-root viewer cannot create the X11 socket
     directory itself.

These tests lock the FIXES in at the source level (offline; no Docker needed):

  * ``Dockerfile.viewer`` EXPLICITLY installs ``chromium-sandbox``;
  * no sandbox-disabling flag exists in any viewer runtime artifact;
  * the sandbox helper is build-validated for existence, root ownership, and
    the setuid mode (4755) — a missing/mis-moded helper FAILS the build;
  * ``/tmp/.X11-unix`` is pre-created with the sticky mode 1777 X11 expects;
  * the container stays uid 1000 / non-root;
  * compose adds no extra privileges, capabilities, security-profile override,
    devices, or published ports;
  * the pre-existing viewer security invariants remain intact.

A minimal pre-fix fixture is run through the same detectors so the checks are
smoke-proof, not decorative.

Runtime image verification (does the REBUILT image actually launch Chromium with
a usable sandbox and a live X socket?) is a VPS step; it is reported as
UNEXECUTED here when Docker is unavailable — these checks are the offline guard,
not a substitute for that step.

Run: python3 -m pytest browser-host/test_viewer_image_sandbox.py
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import viewer_ctl as vc  # noqa: E402

BROWSER_HOST_DIR = Path(__file__).resolve().parent

DOCKERFILE_VIEWER = BROWSER_HOST_DIR / "Dockerfile.viewer"
COMPOSE_VIEWER = BROWSER_HOST_DIR / "docker-compose.viewer.yml"
VIEWER_CTL = BROWSER_HOST_DIR / "viewer_ctl.py"

SANDBOX_PACKAGE = "chromium-sandbox"
# Verified from the trixie .deb:
#   -rwsr-xr-x root/root  ./usr/lib/chromium/chrome-sandbox
SANDBOX_PATH = "/usr/lib/chromium/chrome-sandbox"
SANDBOX_MODE = "4755"                 # setuid root
X11_UNIX_DIR = "/tmp/.X11-unix"
X11_UNIX_MODE = "1777"                # sticky, world-writable

# The literal sandbox-disabling flag must never appear in the viewer path.
FORBIDDEN_FLAG = "--no-sandbox"
# Tokens that must not appear in the viewer compose service.
FORBIDDEN_COMPOSE_TOKENS = (
    "privileged", "cap_add", "security_opt", "devices",
    "no-sandbox", "apparmor=unconfined", "seccomp=unconfined",
)


# ── structural helpers (exercised against the pre-fix fixture too) ─────

def _text(path: Path) -> str:
    return path.read_text()


def _joined(text: str) -> str:
    """Collapse Dockerfile line-continuations so a RUN body is one string."""
    return text.replace("\\\n", " ")


def _installed_packages(dockerfile_text: str) -> set:
    """Every package token an ``apt-get install`` in the file names."""
    joined = _joined(dockerfile_text)
    pkgs: set = set()
    for m in re.finditer(r"apt-get install\b(.*?)(?:&&|\Z)", joined):
        chunk = m.group(1).replace("--no-install-recommends", " ")
        for tok in chunk.split():
            if tok.startswith("-"):
                continue
            pkgs.add(tok)
    return pkgs


def _sandbox_gate_ok(text: str) -> bool:
    """True iff the build validates the sandbox helper fully."""
    j = _joined(text)
    required = [
        SANDBOX_PATH,     # the helper path
        "test -f",        # exists
        "test -u",        # setuid bit present
        "%U:%G",          # ownership stat format
        "root:root",      # ...must equal root:root
        "%a",             # mode stat format
        SANDBOX_MODE,     # ...must equal 4755
    ]
    return all(tok in j for tok in required)


def _x11_gate_ok(text: str) -> bool:
    """True iff the build pre-creates and validates /tmp/.X11-unix."""
    j = _joined(text)
    required = [
        X11_UNIX_DIR,
        "chown root:root",
        "chmod " + X11_UNIX_MODE,
        "%a",
        X11_UNIX_MODE,
    ]
    return all(tok in j for tok in required)


# A minimal, self-contained PRE-FIX fixture: chromium installed WITHOUT
# chromium-sandbox, no helper build-gate, no X11 dir. Deterministic (no git,
# no HEAD) so it also holds in a bare export.
_PREFIX_VIEWER = (
    "FROM python:3.11-slim@sha256:deadbeef\n"
    "ENV DEBIAN_FRONTEND=noninteractive\n"
    "RUN apt-get update \\\n"
    "    && apt-get install -y --no-install-recommends \\\n"
    "        chromium \\\n"
    "        xvfb \\\n"
    "        x11vnc \\\n"
    "        novnc \\\n"
    "        tini \\\n"
    "    && rm -rf /var/lib/apt/lists/*\n"
    "RUN useradd -u 1000 -ms /bin/bash viewer\n"
    "USER viewer\n"
)


# ── 0. the detectors reproduce the pre-fix defect ─────────────────────

def test_prefix_fixture_reproduces_both_blockers_and_production_passes():
    # 1. the pre-fix image is REJECTED by all three detectors…
    pkgs = _installed_packages(_PREFIX_VIEWER)
    assert "chromium" in pkgs
    assert SANDBOX_PACKAGE not in pkgs            # the blocker this fixes
    assert not _sandbox_gate_ok(_PREFIX_VIEWER)
    assert not _x11_gate_ok(_PREFIX_VIEWER)
    # 2. …while the current production Dockerfile passes them.
    prod = _text(DOCKERFILE_VIEWER)
    assert SANDBOX_PACKAGE in _installed_packages(prod)
    assert _sandbox_gate_ok(prod)
    assert _x11_gate_ok(prod)


# ── 1. chromium-sandbox is installed explicitly ───────────────────────

def test_dockerfile_explicitly_installs_chromium_sandbox():
    pkgs = _installed_packages(_text(DOCKERFILE_VIEWER))
    assert SANDBOX_PACKAGE in pkgs, (
        "chromium-sandbox must be installed EXPLICITLY: `chromium` only "
        "Recommends it, so --no-install-recommends omits the setuid helper "
        "and Chromium exits 'No usable sandbox!'")
    # The rest of the viewer stack is unchanged.
    for pkg in ("chromium", "xvfb", "x11vnc", "novnc", "tini"):
        assert pkg in pkgs, pkg
    # The install still uses --no-install-recommends (documented rationale).
    assert "--no-install-recommends" in _text(DOCKERFILE_VIEWER)


# ── 2. no sandbox-disabling flag anywhere in the viewer path ──────────

def test_no_sandbox_disabling_flag_in_any_viewer_artifact():
    for path in (DOCKERFILE_VIEWER, COMPOSE_VIEWER, VIEWER_CTL):
        assert FORBIDDEN_FLAG not in _text(path), (
            f"{path.name} must not carry {FORBIDDEN_FLAG}: the setuid helper is "
            "the capability-free fix, not disabling the sandbox")


def test_viewer_chromium_argv_has_no_sandbox_disabling_flag():
    argv = vc.build_chromium_args("/profiles/bot-b1/owner-me")
    assert not any("sandbox" in a and "no-sandbox" in a for a in argv)
    assert not any("--no-sandbox" == a for a in argv)
    assert not any("no-sandbox" in a for a in argv)
    assert not any(a == "--disable-gpu-sandbox" for a in argv)


# ── 3. the sandbox helper is build-validated ──────────────────────────

def test_sandbox_helper_build_validated_exists_root_owned_setuid():
    text = _text(DOCKERFILE_VIEWER)
    assert _sandbox_gate_ok(text), (
        "Dockerfile.viewer must validate the sandbox helper at build time")
    j = _joined(text)
    assert "'%U:%G'" in j and '"root:root"' in j      # root ownership gate
    assert "'%a'" in j and f'"{SANDBOX_MODE}"' in j   # setuid-mode gate
    assert SANDBOX_PATH in j


def test_sandbox_gate_runs_after_install_and_before_dropping_root():
    text = _text(DOCKERFILE_VIEWER)
    install_at = text.index(SANDBOX_PACKAGE)
    gate_at = text.index(SANDBOX_PATH)
    user_at = text.index("USER viewer")
    assert install_at < gate_at < user_at, (
        "install chromium-sandbox, then validate it as root, then drop to the "
        "non-root viewer user")


# ── 4. /tmp/.X11-unix is pre-created with the secure mode ─────────────

def test_tmp_x11_unix_precreated_with_secure_mode():
    text = _text(DOCKERFILE_VIEWER)
    assert _x11_gate_ok(text), (
        "/tmp/.X11-unix must be pre-created (root:root, mode 1777) and "
        "validated at build time so the non-root Xvfb can create X<display>")
    j = _joined(text)
    assert "mkdir -p " + X11_UNIX_DIR in j
    assert "chown root:root " + X11_UNIX_DIR in j
    assert "chmod " + X11_UNIX_MODE + " " + X11_UNIX_DIR in j


def test_x11_dir_is_created_before_dropping_root():
    text = _text(DOCKERFILE_VIEWER)
    assert text.index(X11_UNIX_DIR) < text.index("USER viewer"), (
        "/tmp/.X11-unix must be created while still root, before USER viewer")


# ── 5. container stays uid 1000 / non-root ────────────────────────────

def test_container_remains_uid_1000_non_root():
    dockerfile = _text(DOCKERFILE_VIEWER)
    assert "useradd -u 1000" in dockerfile
    assert "USER viewer" in dockerfile
    # Nothing re-escalates to root after the drop.
    tail = dockerfile[dockerfile.index("USER viewer"):]
    assert "USER root" not in tail
    # A privileged build-time mode is NOT a privileged runtime user.
    assert "USER 0" not in tail
    # Compose pins the same uid:gid explicitly.
    assert 'user: "1000:1000"' in _text(COMPOSE_VIEWER)


# ── 6. compose adds no privileges/caps/security_opt/devices/ports ─────

def _compose_service() -> dict:
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_text(COMPOSE_VIEWER))
    return doc["services"]["viewer"]


def test_compose_service_sets_no_privileges_caps_security_opt_devices_ports():
    svc = _compose_service()
    for banned in ("privileged", "cap_add", "security_opt", "devices", "ports"):
        assert banned not in svc, f"viewer service must not set `{banned}`"
    # The capability-free posture is preserved: host networking, non-root,
    # no-restart (TTL is the external control boundary).
    assert svc["network_mode"] == "host"
    assert svc["user"] == "1000:1000"
    assert svc["restart"] == "no"


def test_compose_text_carries_no_privilege_or_bypass_tokens():
    text = _text(COMPOSE_VIEWER).lower()
    for tok in FORBIDDEN_COMPOSE_TOKENS:
        assert tok not in text, (
            f"viewer compose must not contain `{tok}` (privilege/bypass "
            "surface)")


# ── 7. pre-existing viewer security invariants remain intact ──────────

def test_viewer_security_invariants_still_hold():
    # No CDP on the headed viewer Chromium.
    argv = vc.build_chromium_args("/profiles/x")
    assert not any("remote-debugging" in a for a in argv)
    # Every listener is loopback-only, and X never listens on TCP.
    assert "-nolisten" in vc.build_xvfb_args()
    xvnc = vc.build_x11vnc_args(":99", 5900, "/tmp/at")
    assert xvnc[xvnc.index("-listen") + 1] == vc.LOOPBACK
    assert "-passwdfile" in xvnc and "-rfbauth" not in xvnc
    ws = vc.build_websockify_args("/usr/share/novnc", 6080, 5900)
    assert ws[-1] == f"{vc.LOOPBACK}:5900"
    assert ws[-2] == f"{vc.LOOPBACK}:6080"
    for a in argv + xvnc + ws:
        assert "0.0.0.0" not in a
    # No published ports in the viewer compose (text-level).
    for line in _text(COMPOSE_VIEWER).splitlines():
        assert not line.strip().startswith("ports:")


# ── 8. runtime image verification is a VPS step, not local ────────────

def test_runtime_image_verification_reported_unexecuted_here():
    """Honest reporting: the rebuilt image is verified on the VPS.

    This suite validates the IMAGE DEFINITION offline. It never builds or runs
    the image, so the true runtime assertions (Chromium finds a usable sandbox;
    Xvfb creates /tmp/.X11-unix/X99 as uid 1000) are executed on the VPS after
    the rebuild. Skip — never a false pass — locally.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker unavailable locally: rebuilt-image runtime "
                    "verification (usable Chromium sandbox + live X socket) is "
                    "UNEXECUTED here and must be run on the VPS")
    pytest.skip("image build/run is a VPS activation step; not built in this "
                "offline suite")
