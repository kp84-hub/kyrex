#!/usr/bin/env python3
"""
fs_executor.py — Kyrex Cloud file-system executor.

Accepts --task <text> on the command line. Supports three operations:
  read <path>             — reads a file and returns its contents.
  write <path> <<< <content>  — writes content to a file (requires approval).
  delete <path>           — removes a regular file (requires approval, tier 2).

Protocol: speaks KYREX_PROGRESS: and KYREX_APPROVAL: lines during work
and exactly one KYREX_RESULT_JSON: line at the end on stdout. Diagnostics
go to stderr.

Path safety: all paths must resolve inside the directory named by the
KYREX_FS_ROOT environment variable (default: /tmp/kyrex-fs). Symlinks
are resolved and any path escaping that root is rejected with an error.
"""
import argparse
import difflib
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


def _handle_read(parts: list[str], root: Path) -> None:
    """Execute a read command. parts is the result of task.split(maxsplit=1)."""
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


def _handle_delete(parts: list[str], root: Path) -> None:
    """Execute a delete command. parts is the result of task.split(maxsplit=1)."""
    if len(parts) < 2 or not parts[1].strip():
        result = {
            "status": "error",
            "final_response": "",
            "errors": ["delete command requires a path argument"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    path_arg = parts[1].strip()

    print(f'KYREX_PROGRESS:{{"delete": {json.dumps(path_arg)}}}', flush=True)

    resolved_path, err = _resolve_safe(path_arg, root)
    if err:
        result = {
            "status": "error",
            "final_response": "",
            "errors": [err],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    # File does not exist — error before approval.
    if not os.path.exists(resolved_path):
        result = {
            "status": "error",
            "final_response": "",
            "errors": [f"file not found: {path_arg}"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    # Reject directories — only regular files may be deleted.
    if not os.path.isfile(resolved_path):
        result = {
            "status": "error",
            "final_response": "",
            "errors": [f"not a regular file: {path_arg}"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    # Gather detail: size in bytes + first 10 lines.
    st = os.stat(resolved_path)
    size = st.st_size
    try:
        with open(resolved_path, "r") as f:
            first_10_lines = "".join(f.readlines()[:10])
    except Exception:
        first_10_lines = "(could not read file preview)"

    detail = f"{size} bytes\n{first_10_lines}"

    basename = os.path.basename(resolved_path)
    approval = {
        "tier": 2,
        "summary": f"delete {path_arg}",
        "token": f"DELETE {basename}",
        "detail": detail,
    }
    print(f"KYREX_APPROVAL:{json.dumps(approval)}", flush=True)

    decision = sys.stdin.readline().strip()

    if decision == "APPROVED":
        try:
            os.remove(resolved_path)
        except OSError as e:
            result = {
                "status": "error",
                "final_response": "",
                "errors": [f"error deleting {path_arg}: {e}"],
            }
            print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
            return
        result = {
            "status": "ok",
            "final_response": f"deleted {path_arg}",
            "errors": [],
        }
    else:
        result = {
            "status": "error",
            "final_response": "",
            "errors": [f"delete denied for {path_arg}"],
        }

    print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)


def _handle_write(parts: list[str], root: Path) -> None:
    """Execute a write command. parts is the result of task.split(maxsplit=1).

    Format: write <path> <<< <content>
    """
    if len(parts) < 2 or not parts[1].strip():
        result = {
            "status": "error",
            "final_response": "",
            "errors": ["write command requires '<path> <<< <content>' arguments"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    remainder = parts[1].strip()

    # Split on " <<< " delimiter
    if " <<< " not in remainder:
        result = {
            "status": "error",
            "final_response": "",
            "errors": [
                "unsupported write format — expected 'write <path> <<< <content>'"
            ],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    path_arg, content = remainder.split(" <<< ", maxsplit=1)

    # Resolve path safely before any approval request.
    resolved_path, err = _resolve_safe(path_arg, root)
    if err:
        result = {
            "status": "error",
            "final_response": "",
            "errors": [err],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    # Build the approval detail: unified diff if the file exists,
    # otherwise first 20 lines of new content.
    content_bytes = content.encode("utf-8")
    detail = ""
    if os.path.exists(resolved_path):
        try:
            with open(resolved_path, "r") as f:
                existing = f.read()
            diff_lines = list(
                difflib.unified_diff(
                    existing.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile="current",
                    tofile="proposed",
                )
            )
            detail = "".join(diff_lines)
        except Exception:
            detail = "(could not compute diff)"
    else:
        # New file: show first 20 lines.
        lines = content.splitlines()
        detail = "\n".join(lines[:20])

    approval = {
        "tier": 1,
        "summary": f"write {path_arg} ({len(content_bytes)} bytes)",
        "detail": detail,
    }
    print(f"KYREX_APPROVAL:{json.dumps(approval)}", flush=True)

    # Block waiting for approval from stdin.
    decision = sys.stdin.readline().strip()

    if decision == "APPROVED":
        # Ensure parent directory exists, then write.
        Path(resolved_path).parent.mkdir(parents=True, exist_ok=True)
        with open(resolved_path, "w") as f:
            f.write(content)
        result = {
            "status": "ok",
            "final_response": f"wrote {len(content_bytes)} bytes to {path_arg}",
            "errors": [],
        }
    else:
        result = {
            "status": "error",
            "final_response": "",
            "errors": [f"write denied for {path_arg}"],
        }

    print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)


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

    # Parse command: expect "read <path>" or "write <path> <<< <content>"
    parts = task.split(maxsplit=1)
    if not parts:
        result = {
            "status": "error",
            "final_response": "",
            "errors": ["empty task"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    cmd = parts[0].lower()

    if cmd == "read":
        _handle_read(parts, root)
    elif cmd == "write":
        _handle_write(parts, root)
    elif cmd == "delete":
        _handle_delete(parts, root)
    else:
        result = {
            "status": "error",
            "final_response": "",
            "errors": [f"unsupported command: {parts[0] if parts else '(empty)'} — supported commands are 'read', 'write', and 'delete'"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return


if __name__ == "__main__":
    main()