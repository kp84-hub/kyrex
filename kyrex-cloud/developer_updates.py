"""Small owner-facing projections of developer activity, never tool payloads."""
import re

DEVELOPER_EXECUTORS = {"developer", "repo"}


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
        if name == "run_command":
            return "Command finished; reviewing the result…"
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
        if isinstance(command, str) and re.search(r"\b(?:pytest|unittest|vitest|jest)\b|\bnpm (?:run )?(?:test|build)\b|\bgo test\b", command):
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
            out.append({"stage": stage, "category": "developer"})
    return out[-100:]
