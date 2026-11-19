"""Owner-facing developer activity and verification, without raw tool output."""
import re

DEVELOPER_EXECUTORS = {"developer", "repo", "rift_sandbox"}
_CHECK_COMMANDS = {
    "tests": r"(?:pytest|python3? -m (?:pytest|unittest)|npm (?:run )?test|pnpm (?:run )?test|yarn test|go test|cargo test|node --test|npx (?:playwright test|vitest run))",
    "build": r"(?:(?:npm|pnpm|yarn) (?:run )?build|go build|cargo build|vite build|npx vite build)",
    "lint": r"(?:(?:npm|pnpm|yarn) (?:run )?lint|ruff check|npx eslint)",
    "diff": r"git diff (?:--cached )?--check",
}
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPOSITORY = re.compile(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+\Z")
_FRESHNESS = {"current", "updated", "local_commits", "behind", "diverged", "unverified"}
_OUTCOMES = {"success", "failure", "timeout", "unavailable"}
_CHECK_STATES = {"passed", "failed", "pending", "other"}
_MAX_CHECKS = 12


def check_command(command):
    # Record only a stand-alone recognized command. Shell chains can hide
    # which part failed or even force a success status after a failed check.
    if not isinstance(command, str) or re.search(r"[\n\r;|&<>`$()\\]", command):
        return None
    command = " ".join(command.split())
    for kind, pattern in _CHECK_COMMANDS.items():
        match = re.match(pattern + r"(?= |$)", command)
        if match:
            return kind, match[0]
    return None


def observed_check(start, result):
    """Derive a small receipt from a matching tool start and tool result."""
    args = start.get("args")
    if not isinstance(args, dict) or not isinstance(result, dict):
        return None
    if start.get("name") == "run_command":
        check = check_command(args.get("command"))
        if not check:
            return None
        kind, runner = check
        if result.get("error"):
            return {"kind": kind, "runner": runner,
                    "outcome": "timeout" if result.get("error_type") == "command_timeout" else "unavailable"}
        code = result.get("returncode", result.get("exit_code"))
        if type(code) is int and -255 <= code <= 255:
            return {"kind": kind, "runner": runner,
                    "outcome": "success" if code == 0 else "failure", "exit_code": code}
    if start.get("name") != "github_read" or args.get("action") != "checks" or result.get("error"):
        return None
    repository, sha = result.get("repository"), result.get("sha")
    if repository != args.get("repository") or sha != str(args.get("ref", "")).lower():
        return None
    counts = dict.fromkeys(sorted(_CHECK_STATES), 0)
    for rows, name in ((result.get("check_runs"), "conclusion"), (result.get("status_contexts"), "state")):
        if not isinstance(rows, list):
            return None
        for row in rows[:25]:
            if not isinstance(row, dict):
                continue
            state = row.get(name)
            if state == "success":
                bucket = "passed"
            elif state in ("failure", "error", "action_required", "timed_out", "cancelled", "startup_failure", "stale"):
                bucket = "failed"
            elif state == "pending" or (name == "conclusion" and row.get("status") in ("queued", "in_progress", "requested", "waiting", "pending")):
                bucket = "pending"
            else:
                bucket = "other"
            counts[bucket] += 1
    return {"kind": "github", "repository": repository, "sha": sha, "counts": counts,
            "partial": bool(result.get("unavailable_permissions") or result.get("truncated"))}


def public_verification(result):
    """Project a versioned runner result for the owner without exposing its tools."""
    if not isinstance(result, dict) or result.get("mode") != "developer" or "verification" not in result:
        return None
    verification = result.get("verification")
    if not isinstance(verification, list):
        return None
    checkout = result.get("workspace_freshness")
    if not isinstance(checkout, dict):
        checkout = {}
    head, remote = checkout.get("head"), checkout.get("remote_head")
    view = {"checks": []}
    if isinstance(head, str) and _SHA.fullmatch(head):
        freshness = checkout.get("status")
        view.update(head=head, freshness=freshness if isinstance(freshness, str) and freshness in _FRESHNESS else "unverified")
        if isinstance(remote, str) and _SHA.fullmatch(remote) and remote != head:
            view["remote_head"] = remote
    for row in verification[-_MAX_CHECKS:]:
        if not isinstance(row, dict) or not isinstance(row.get("kind"), str):
            continue
        kind = row.get("kind")
        if kind in _CHECK_COMMANDS and isinstance(row.get("outcome"), str) and row["outcome"] in _OUTCOMES:
            code = row.get("exit_code")
            runner = row.get("runner")
            view["checks"].append({"kind": kind, "outcome": row["outcome"],
                                  **({"runner": runner} if isinstance(runner, str)
                                     and re.fullmatch(_CHECK_COMMANDS[kind], runner) else {}),
                                  **({"exit_code": code} if type(code) is int and -255 <= code <= 255 else {})})
        elif kind == "github":
            repository, sha, counts = row.get("repository"), row.get("sha"), row.get("counts")
            if (isinstance(repository, str) and len(repository) <= 200 and _REPOSITORY.fullmatch(repository)
                    and isinstance(sha, str) and _SHA.fullmatch(sha) and isinstance(counts, dict)
                    and all(type(counts.get(key)) is int and 0 <= counts[key] <= 50 for key in _CHECK_STATES)):
                view["checks"].append({"kind": kind, "url": f"https://github.com/{repository}/commit/{sha}/checks",
                                       "counts": {key: counts[key] for key in sorted(_CHECK_STATES)},
                                       "partial": row.get("partial") is True})
    return view


def clean_update(text):
    from kyrex.providers.privacy import SecretFilter
    text = SecretFilter().text(str(text or ""))
    # Engine markup and fenced output are not conversational updates.
    if re.search(r"</?(?:invoke|function_calls|parameter|tool_call)\b|\[Task Complete|```", text, re.I):
        return ""
    return " ".join(text.split())[:240]


def tool_stage(event):
    kind = event.get("type")
    name = event.get("name")
    if kind == "propose_edit":
        return "Reviewing a file edit…"
    if kind == "tool_result":
        result = event.get("result")
        if isinstance(result, dict):
            if result.get("error"):
                # Fixed descriptions only: never relay commands, paths, tokens
                # or provider/tool text into owner-facing activity.
                error_type = result.get("error_type")
                return {
                    "command_timeout": "Command reached its time limit; checking partial results.",
                    "file_not_found": "File was not found; checking its path.",
                    "file_unreadable": "File could not be read; checking access.",
                    "file_too_large": "File is too large for a full read; narrowing the search.",
                    "invalid_arguments": "Tool arguments were invalid; correcting the request.",
                    "access_denied": "File access was denied; checking the allowed workspace.",
                    "terminal_confirmation": "Command needs terminal confirmation; choosing another approach.",
                }.get(error_type if isinstance(error_type, str) else "", "A tool failed; checking how to proceed.")
            code = result.get("exit_code", result.get("returncode", 0))
            if code not in (0, None):
                return (f"Command exited with code {code}; reviewing the result."
                        if type(code) is int and -255 <= code <= 255 else "A tool failed; checking how to proceed.")
            if name == "read_local_file" and result.get("truncated"):
                return "Read part of a file; more content is available."
        return ""
    if kind != "tool_start" or name == "task_complete":
        return ""
    if name in {"read_local_file", "read_file", "list_local_files", "list_directory", "list_files", "search"}:
        return "Inspecting the relevant files…"
    if name in {"edit_file", "write_file_with_gate", "write_file"}:
        return "Updating the code…"
    if name == "run_command":
        args = event.get("args")
        command = args.get("command", "") if isinstance(args, dict) else ""
        check = check_command(command)
        if check and check[0] == "build":
            return "Building the project…"
        if check:
            return "Running checks…"
        return "Running a workspace command…"
    return "Using a development tool…"


def public_progress(notes):
    """Bounded stage-only history; callers must verify task ownership first."""
    out = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        stage = clean_update(note.get("stage")) if isinstance(note.get("stage"), str) else ""
        if stage and (not out or out[-1]["stage"] != stage):
            safe = {"stage": stage, "category": "developer"}
            if type(note.get("event_id")) is int:
                safe["event_id"] = note["event_id"]
            out.append(safe)
    return out[-100:]
