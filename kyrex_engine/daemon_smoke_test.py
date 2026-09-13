#!/usr/bin/env python3
"""Manual integration smoke test for daemon mode (background engine).

Run from kyrex_engine/ after installing engine deps:

    python3 daemon_smoke_test.py

Verifies the full daemon lifecycle of core_bridge.py:

1. Spawns core_bridge.py with KYREX_DAEMON=1 and a stub workspace config.
2. Waits for the daemon control file (~/.kyrex/daemons/{key}.json).
3. Attaches a TCP client, expects a session_replay marker + buffered lines.
4. Sends {"type":"shutdown"}.
5. Asserts the process exits cleanly and removes its control file.

The stub provider key is dummy — no API call is made; we only prove the
lifecycle (boot, discovery, attach, replay, shutdown, cleanup) works.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent


def fail(msg, log_path=None):
    print(f"FAIL: {msg}")
    if log_path and Path(log_path).exists():
        print("--- daemon log (tail) ---")
        print(Path(log_path).read_text(errors="replace")[-2000:])
    sys.exit(1)


def main():
    home = Path.home()  # control files land under the real home
    # Unique workspace per run → unique daemon key → no collision with
    # control files left behind by earlier (crashed) runs.
    ws = Path(tempfile.mkdtemp(prefix="kyrex-smoke-ws-"))
    (ws / ".px").mkdir(parents=True)
    (ws / ".px_sessions").mkdir(parents=True)
    (ws / ".px" / "config.json").write_text(json.dumps({
        "provider": "openai",
        "api_key": "sk-dummy-smoke-test",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    }))

    log_path = ws / "daemon.log"
    log = open(log_path, "wb")
    env = dict(os.environ)
    env.update({
        "KYREX_DAEMON": "1",
        "KYREX_SURFACE": "smoke-test",
        "KYREX_VSCODE": "1",
        "WORKSPACE_ROOT": str(ws),
    })
    proc = subprocess.Popen(
        [sys.executable, str(ENGINE_DIR / "core_bridge.py")],
        cwd=str(ws), env=env, stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )

    try:
        # 1. Control file appears (exact file for THIS workspace — the dir
        # may hold stale files from other workspaces/runs).
        sys.path.insert(0, str(ENGINE_DIR))
        from daemon_bridge import control_file_path
        ctrl = control_file_path(str(ws))
        info = None
        deadline = time.time() + 25
        while time.time() < deadline:
            if proc.poll() is not None:
                fail(f"daemon exited early with code {proc.returncode}", log_path)
            if ctrl.exists():
                info = json.loads(ctrl.read_text())
                break
            time.sleep(0.2)
        if not info:
            fail("control file never appeared", log_path)
        if info["pid"] != proc.pid:
            fail(f"control file pid {info['pid']} != spawned pid {proc.pid}", log_path)
        print(f"OK control file: pid={info['pid']} port={info['port']}")

        # 2. Attach → session_replay marker + buffered lines
        try:
            sock = socket.create_connection(("127.0.0.1", info["port"]), timeout=10)
        except OSError:
            if proc.poll() is not None:
                fail(f"daemon died (code {proc.returncode}) before accepting", log_path)
            fail(f"could not connect to 127.0.0.1:{info['port']}", log_path)
        sock.settimeout(10)
        buf = b""
        lines = []
        deadline = time.time() + 10
        while len(lines) < 2 and time.time() < deadline:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                if raw.strip():
                    lines.append(raw.decode())
        if not lines:
            fail("no replay on attach", log_path)
        marker = json.loads(lines[0])
        if marker.get("type") != "session_replay":
            fail(f"expected session_replay marker, got: {lines[0][:150]}", log_path)
        types = []
        for raw in lines[1:]:
            try:
                types.append(json.loads(raw).get("type"))
            except json.JSONDecodeError:
                pass
        if "session_state" not in types:
            fail(f"expected session_state in replay, got types: {types}", log_path)
        print(f"OK attach: replay count={marker['count']} branch={marker.get('branch')!r} "
              f"types={types}")

        # 3. Graceful shutdown
        sock.sendall(json.dumps({"type": "shutdown"}).encode() + b"\n")
        sock.close()

        # 4. Clean exit + control file removed
        deadline = time.time() + 15
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.2)
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
            fail("daemon did not exit on shutdown message (SIGTERM fallback used)", log_path)
        if proc.returncode != 0:
            fail(f"daemon exit code {proc.returncode}", log_path)
        if ctrl.exists():
            fail(f"control file not removed on exit: {ctrl}", log_path)
        print("OK shutdown: clean exit(0), control file removed")
        print("ALL SMOKE TESTS PASSED")
    except Exception as e:
        fail(f"unexpected error: {e}", log_path)
    finally:
        if proc.poll() is None:
            proc.kill()
        log.close()
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    main()
