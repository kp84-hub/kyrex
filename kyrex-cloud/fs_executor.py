#!/usr/bin/env python3
"""
fs_executor.py — Kyrex Cloud file-system executor.

Accepts --task <text> on the command line. Supports one operation:
  read <path>   — reads a file and returns its contents.

Protocol: speaks KYREX_PROGRESS: lines during work and exactly one
KYREX_RESULT_JSON: line at the end on stdout. Diagnostics go to stderr.

Path safety: all paths must resolve inside the directory named by the
KYREX_FS_ROOT environment variable (default: /tmp/kyrex-fs). Symlinks
are resolved and any path escaping that root is rejected with an error.
"""
import argparse
import json
import os
import sys
from pathlib import Path


def _resolve_safe(requested: str, root: Path) -> tuple[str | None, str | None]:
    """Resolve a user-supplied path relative to *root*.

    Returns (resolved_str, None) on success, or (None, error_message) if
    the path escapes the root (via .., symlink, or an absolute path that
    doesn't live under root).
    """
    candidate = root / requested
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as e:
        return None, f"path resolution failed: {e}"

    # Both must be real for the prefix check to be trustworthy.
    root_real = root.resolve(strict=False)

    if not str(resolved).startswith(str(root_real) + "/") and resolved != root_real:
        return None, (
            f"path escapes the filesystem root: {requested!r} resolves to "
            f"{resolved}, which is outside {root_real}"
        )

    return str(resolved), None


def main():
    ap = argparse.ArgumentParser(description="Kyrex Cloud FS Executor")
    ap.add_argument("--task", required=True, help="task text, e.g. 'read /path/file.txt'")
    # run_task in serve.py passes --repo-url and --base to every executor;
    # accept but ignore them here so the calling convention is uniform.
    ap.add_argument("--repo-url", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--base", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    task = args.task.strip()

    root_raw = os.environ.get("KYREX_FS_ROOT", "/tmp/kyrex-fs")
    root = Path(root_raw).resolve()

    if not root.exists():
        result = {
            "status": "error",
            "final_response": "",
            "errors": [f"KYREX_FS_ROOT directory does not exist: {root}"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    # Parse command: expect "read <path>"
    parts = task.split(maxsplit=1)
    if not parts or parts[0].lower() != "read":
        result = {
            "status": "error",
            "final_response": "",
            "errors": [f"unsupported command: {parts[0] if parts else '(empty)'} — only 'read' is supported so far"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    if len(parts) < 2 or not parts[1].strip():
        result = {
            "status": "error",
            "final_response": "",
            "errors": ["read command requires a path argument"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    path_arg = parts[1].strip()

    # Progress line so the host knows we're working.
    print(f'KYREX_PROGRESS:{{"read": {json.dumps(path_arg)}}}', flush=True)

    resolved_path, err = _resolve_safe(path_arg, root)
    if err:
        result = {
            "status": "error",
            "final_response": "",
            "errors": [err],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    try:
        with open(resolved_path, "r") as f:
            content = f.read()
    except FileNotFoundError:
        result = {
            "status": "error",
            "final_response": "",
            "errors": [f"file not found: {path_arg}"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return
    except IsADirectoryError:
        result = {
            "status": "error",
            "final_response": "",
            "errors": [f"path is a directory, not a file: {path_arg}"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return
    except PermissionError:
        result = {
            "status": "error",
            "final_response": "",
            "errors": [f"permission denied: {path_arg}"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return
    except OSError as e:
        result = {
            "status": "error",
            "final_response": "",
            "errors": [f"error reading file {path_arg}: {e}"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    result = {
        "status": "ok",
        "final_response": content,
        "errors": [],
    }
    print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)


if __name__ == "__main__":
    main()