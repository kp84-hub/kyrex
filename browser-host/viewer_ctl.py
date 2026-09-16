"""viewer_ctl.py — control boundary for the manual Browser Host viewer.

This is the ONLY file that starts the manual viewer stack. It has two modes:

  * ``hold`` — runs INSIDE the viewer container and owns the entire lifecycle
    of one manual session: acquire the exclusive per-profile lock
    (:mod:`manual_mode`, ``KIND_MANUAL`` — the SAME lock the automation agent
    takes), start Xvfb, then a HEADED Chromium against the existing
    ``(owner, bot_id)`` persistent profile, then ``x11vnc`` and
    ``websockify``/noVNC — every listener on ``127.0.0.1`` ONLY — and tear
    everything down at the TTL deadline or on SIGTERM/SIGINT. When this process
    dies for ANY reason (crash, OOM, container stop, tini relaying a signal to
    the process group), the kernel flock drops with it: automation eligibility
    returns and the profile is simply not used by anything. The X/Chromium/VNC
    children are each started in their OWN process group and torn down as a tree
    (SIGTERM to the group, then SIGKILL), so no headed Chromium survives a
    session.
  * ``start`` / ``end`` / ``status`` / ``reap`` — run on the VPS as the owner
    user. ``start`` validates the parameters and shells out to
    ``docker compose -f docker-compose.viewer.yml --profile viewer up -d``
    (overridable for tests via ``_run_compose``). It publishes NO port and
    exposes NO viewer route through Cloud/Railway — reachability is entirely
    Tailscale Serve + tailnet ACLs, provisioned separately at activation.

Security shape (asserted by ``test_viewer.py`` / ``test_viewer_lock.py``):

  * every listening socket this code constructs is loopback; there is no
    ``ports:`` mapping anywhere; ``network_mode: host`` makes the loopback
    bindings the HOST's loopback so ``tailscale serve`` can front noVNC —
    still local-only, never routable beyond the tailnet;
  * the viewer Chromium opens NO CDP listener at all (no
    ``--remote-debugging-*``): the human IS the driver;
  * a VNC password is mandatory — the viewer refuses to start without one
    (fail closed), and the 0600 file it is read from is re-checked for mode
    0600. The password is handed to ``x11vnc -rfbauth`` from a 0600 file that
    is deleted when the session ends; it is never printed or logged;
  * the process refuses to run as root;
  * this process has NO Cloud configuration and no egress logic: nothing —
    credential, keystroke, cookie, DOM byte, screenshot — can reach Railway.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import manual_mode  # noqa: E402
import profiles  # noqa: E402

# Every listener is loopback-only. Tests pin these literals.
LOOPBACK = "127.0.0.1"
DEFAULT_DISPLAY = ":99"
DEFAULT_VNC_PORT = 5900
DEFAULT_NOVNC_PORT = 6080
NOVNC_WEB_ROOT = "/usr/share/novnc"

# Short-lived by construction: the record TTL caps expiry AND `hold` kills its
# own children at the deadline, whichever comes first.
MAX_TTL = 3600.0  # one hour — manual control is bounded by design

CHROMIUM_BIN = "/usr/bin/chromium"
XVFB_BIN = "/usr/bin/Xvfb"
X11VNC_BIN = "/usr/bin/x11vnc"
WEBSOCKIFY_BIN = "/usr/bin/websockify"

# Viewer Chromium flags: headed window, NO CDP anywhere in this list.
CHROMIUM_FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-crashpad",
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
]

_BROWSER_HOST_DIR = Path(__file__).resolve().parent


class ViewerError(Exception):
    """The viewer cannot/should not start (fail closed)."""


# ── Pure argv builders (unit-tested: the argv IS the security shape) ───

def build_xvfb_args(display: str = DEFAULT_DISPLAY) -> list:
    """Xvfb as a virtual screen; ``-nolisten tcp`` keeps X off the network."""
    return [XVFB_BIN, display, "-screen", "0", "1440x900x24", "-nolisten", "tcp"]


def build_chromium_args(profile_dir: str) -> list:
    """The headed viewer Chromium — deliberately NO CDP flags at all."""
    return [CHROMIUM_BIN, f"--user-data-dir={profile_dir}", *CHROMIUM_FLAGS]


def build_x11vnc_args(display: str, port: int, auth_file: str,
                      loopback: str = LOOPBACK) -> list:
    return [
        X11VNC_BIN,
        "-display", display,
        "-rfbport", str(port),
        "-listen", loopback,   # bind the listening socket to loopback ONLY
        "-rfbauth", auth_file,
        "-shared", "-forever",
        "-noxrecord", "-noxfixes", "-noxdamage",
        "-quiet",
    ]


def build_websockify_args(web_root: str, listen_port: int, vnc_port: int,
                          loopback: str = LOOPBACK) -> list:
    return [
        WEBSOCKIFY_BIN, "--web", web_root,
        f"{loopback}:{listen_port}",   # noVNC: loopback only
        f"{loopback}:{vnc_port}",      # VNC upstream: loopback only
    ]


# ── shared state location ─────────────────────────────────────────────

def host_state_dir() -> str:
    """The host-side path of the SHARED state dir (the profiles-volume bind).

    In the containers this resolves from ``KYREX_VIEWER_STATE_DIR``; the
    VPS-side control commands fall back to ``<browser-host>/profiles/<state>``,
    which is the host bind source of the same volume the containers mount, so
    the control commands see the exact records the containers write.
    """
    env = os.environ.get(manual_mode.STATE_DIR_ENV, "").strip()
    if env:
        return env
    return str(_BROWSER_HOST_DIR / "profiles" / manual_mode.STATE_DIR_NAME)


# ── hold (in-container runtime) ───────────────────────────────────────

def _vnc_password() -> str:
    """Fail-closed VNC password: env var or 0600 file; never logged.

    A password file with any group/other permission bit set is refused — the
    viewer must not start from a world-readable secret.
    """
    env_pw = os.environ.get("KYREX_VIEWER_VNC_PASSWORD", "")
    if env_pw:
        return env_pw
    pw_file = str(os.environ.get("KYREX_VIEWER_VNC_PASSWORD_FILE") or "")
    if pw_file:
        path = Path(pw_file)
        if path.is_file():
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                raise ViewerError(
                    "the VNC password file must be mode 0600 (no group/other "
                    "bits); refusing to start")
            content = path.read_text().rstrip("\n")
            if content:
                return content
    raise ViewerError(
        "KYREX_VIEWER_VNC_PASSWORD (or _FILE) is required: the viewer must "
        "not start without an authenticated VNC endpoint")


def _spawn(cmd: list, env: dict | None = None) -> subprocess.Popen:
    """Start one viewer child in its OWN process group; output is unlogged.

    ``start_new_session`` makes the child a session/group leader so the whole
    tree (Xvfb, Chromium and all of its renderer/zygote children, x11vnc,
    websockify) can be signalled together and none is left orphaned.
    """
    return subprocess.Popen(cmd, env=env if env is not None
                            else os.environ.copy(), start_new_session=True)


def _signal_tree(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except Exception:  # noqa: BLE001 — the child may already be gone
        try:
            proc.send_signal(sig)
        except Exception:  # noqa: BLE001
            pass


def _terminate_all(children: list) -> None:
    for proc in reversed(list(children)):
        _signal_tree(proc, signal.SIGTERM)
    for proc in reversed(list(children)):
        try:
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            _signal_tree(proc, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
    children.clear()


def hold() -> int:
    """Run ONE manual session to completion. Returns the process exit code.

    Guarantees:
      * refuses as root (exit 4);
      * refuses with no VNC password, or a non-0600 password file (exit 3);
      * refuses when the profile is already under automation or manual control
        (exit 3) — the acquire takes the SAME flock the agent takes, so the two
        can never run against one profile;
      * the exclusive flock lasts exactly as long as this process;
      * children are terminated (as process groups) and reaped at the TTL
        deadline, on SIGTERM/SIGINT, and in the finally on any path;
      * nothing is ever sent to Cloud — no such configuration exists here.
    """
    if os.getuid() == 0:
        print("[viewer] refusing to run the manual viewer as root",
              file=sys.stderr)
        return 4

    owner = str(os.environ.get("KYREX_VIEWER_OWNER") or "").strip()
    bot_id = str(os.environ.get("KYREX_VIEWER_BOT") or "").strip()
    if not owner or not bot_id:
        print("[viewer] KYREX_VIEWER_OWNER and KYREX_VIEWER_BOT are required",
              file=sys.stderr)
        return 2

    raw_ttl = str(os.environ.get("KYREX_VIEWER_TTL")
                  or manual_mode.DEFAULT_TTL)
    try:
        ttl = float(raw_ttl)
    except (TypeError, ValueError):
        ttl = manual_mode.DEFAULT_TTL
    ttl = min(max(ttl, 60.0), MAX_TTL)

    try:
        vnc_password = _vnc_password()
    except ViewerError as exc:
        print(f"[viewer] {exc}", file=sys.stderr)
        return 3

    profile_dir = str(profiles.ensure_profile(owner, bot_id))
    display = str(os.environ.get("KYREX_VIEWER_DISPLAY") or DEFAULT_DISPLAY)
    display_number = display.lstrip(":")
    vnc_port = int(os.environ.get("KYREX_VIEWER_VNC_PORT")
                   or DEFAULT_VNC_PORT)
    novnc_port = int(os.environ.get("KYREX_VIEWER_NOVNC_PORT")
                     or DEFAULT_NOVNC_PORT)

    auth_file = Path("/tmp") / f"kyrex-viewer-auth-{secrets.token_hex(8)}"
    auth_file.write_bytes(vnc_password.encode() + b"\n")
    os.chmod(auth_file, 0o600)

    session = None
    children: list[subprocess.Popen] = []
    stop_evt = threading.Event()

    def _on_signal(_signum, _frame):
        stop_evt.set()

    def _deadline_watcher():
        stop_evt.wait(timeout=ttl)
        stop_evt.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    exit_code = 0
    try:
        try:
            session = manual_mode.acquire(
                owner, bot_id, ttl=ttl, kind=manual_mode.KIND_MANUAL,
                meta={"novnc_port": novnc_port, "vnc_port": vnc_port})
        except manual_mode.ManualControlActive as exc:
            print(f"[viewer] {exc}", file=sys.stderr)
            return 3
        except manual_mode.ViewerPathError as exc:
            print(f"[viewer] refusing to start: {exc}", file=sys.stderr)
            return 3

        children.append(_spawn(build_xvfb_args(display)))
        x_socket = Path("/tmp/.X11-unix") / f"X{display_number}"
        xvfb_deadline = time.time() + 15
        while not x_socket.exists() and time.time() < xvfb_deadline:
            if any(proc.poll() is not None for proc in children):
                print("[viewer] Xvfb exited before its display was ready",
                      file=sys.stderr)
                return 5
            time.sleep(0.1)
        if not x_socket.exists():
            exit_code = 5
            print("[viewer] Xvfb never created its display socket",
                  file=sys.stderr)
            return exit_code

        chromium_env = os.environ.copy()
        chromium_env["DISPLAY"] = display
        chromium_env.setdefault("HOME", "/home/viewer")
        children.append(_spawn(build_chromium_args(profile_dir), chromium_env))
        children.append(_spawn(build_x11vnc_args(
            display, vnc_port, str(auth_file))))
        children.append(_spawn(build_websockify_args(
            NOVNC_WEB_ROOT, novnc_port, vnc_port)))

        print(
            f"[viewer] manual session {session.session_id()} active for "
            f"owner={owner!r} bot={bot_id!r}; noVNC loopback "
            f"http://{LOOPBACK}:{novnc_port}/ (tailnet-only); TTL {int(ttl)}s",
            flush=True)

        threading.Thread(target=_deadline_watcher, daemon=True).start()

        while not stop_evt.is_set():
            for proc in children:
                if proc.poll() is not None:
                    # A service died early: the session must not linger with
                    # a half-alive surface; tear it all down.
                    stop_evt.set()
                    exit_code = 6
                    break
            if not stop_evt.is_set():
                time.sleep(0.5)

        _terminate_all(children)
        return exit_code
    finally:
        # Everything releases in every path: children, lock, password file.
        try:
            _terminate_all(children)
        except Exception:  # noqa: BLE001
            pass
        if session is not None:
            try:
                session.release()
            except Exception:  # noqa: BLE001
                pass
        try:
            auth_file.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


# ── host-side conveniences ────────────────────────────────────────────

def _compose_file() -> Path:
    return _BROWSER_HOST_DIR / "docker-compose.viewer.yml"


def _run_compose(args: list, env: dict) -> int:
    return subprocess.run(args, env=env, check=False).returncode


def compose_up_args() -> list:
    return [
        "docker", "compose", "-f", str(_BROWSER_HOST_DIR
                                       / "docker-compose.viewer.yml"),
        "--profile", "viewer", "up", "-d", "viewer",
    ]


def compose_down_args() -> list:
    return [
        "docker", "compose", "-f", str(_BROWSER_HOST_DIR
                                       / "docker-compose.viewer.yml"),
        "--profile", "viewer", "down",
    ]


def _active_env(owner: str, bot_id: str, ttl: float) -> dict:
    env = os.environ.copy()
    env["KYREX_VIEWER_OWNER"] = str(owner).strip()
    env["KYREX_VIEWER_BOT"] = str(bot_id).strip()
    env["KYREX_VIEWER_TTL"] = str(min(max(float(ttl), 60.0), MAX_TTL))
    return env


def cmd_start(args) -> int:
    # Best-effort fast-fail for an obvious duplicate; the AUTHORITATIVE
    # acquisition is `hold` inside the container, which takes the SAME flock
    # and refuses if automation (or another viewer) owns the profile. This
    # check is convenience, not the exclusivity guarantee.
    if manual_mode.active(args.owner, args.bot, root=args.state_dir):
        print("[viewer] ManualControlActive: a manual viewer session is "
              "already running for this profile", file=sys.stderr)
        return 3
    code = _run_compose(compose_up_args(),
                        _active_env(args.owner, args.bot, args.ttl))
    if code != 0:
        print("[viewer] compose up failed — nothing was left behind with "
              "manual-mode still active", file=sys.stderr)
        return code
    ttl = min(max(float(args.ttl), 60.0), MAX_TTL)
    print(f"[viewer] manual viewer starting for owner={args.owner} "
          f"bot={args.bot}; it opens, expires, and auto-shuts-down at "
          f"ttl={int(ttl)}s on its own")
    return 0


def cmd_end(args) -> int:
    code = _run_compose(compose_down_args(), _active_env("", "", 0))
    reaped = manual_mode.reap_expired(root=args.state_dir)
    print(f"[viewer] viewer stack stopped (exit {code}); reaped: "
          f"{reaped or 'none'}", file=sys.stderr)
    return 0 if code == 0 else code


def cmd_status(args) -> int:
    print(json.dumps(manual_mode.cli_status_json(root=args.state_dir),
                     sort_keys=True))
    return 0


def cmd_reap(args) -> int:
    reaped = manual_mode.reap_expired(root=args.state_dir)
    print(f"reaped: {reaped}")
    return 0


def _cmd_hold(_args) -> int:
    return hold()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Manual viewer control boundary for the Kyrex Browser "
                    "Host (Tailscale-only, loopback-only, non-root).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    hold_p = sub.add_parser("hold", help="(in-container) run one session")
    hold_p.set_defaults(func=_cmd_hold)

    def _owner_bot(parser):
        parser.add_argument("--owner", required=True)
        parser.add_argument("--bot", required=True)
        parser.add_argument("--ttl", type=float,
                            default=manual_mode.DEFAULT_TTL)
        parser.add_argument("--state-dir", default=host_state_dir())

    start = sub.add_parser("start", help="(VPS) launch the viewer stack")
    _owner_bot(start)
    start.set_defaults(func=cmd_start)

    end = sub.add_parser("end", help="(VPS) stop the viewer + reap dead state")
    end.add_argument("--state-dir", default=host_state_dir())
    end.set_defaults(func=cmd_end)

    status = sub.add_parser("status", help="print live manual sessions")
    status.add_argument("--state-dir", default=host_state_dir())
    status.set_defaults(func=cmd_status)

    reap = sub.add_parser("reap", help="delete dead manual-mode records")
    reap.add_argument("--state-dir", default=host_state_dir())
    reap.set_defaults(func=cmd_reap)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except manual_mode.ManualControlActive as exc:
        print(f"[viewer] {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
