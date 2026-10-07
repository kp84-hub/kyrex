"""Host-defined expectations for fixed read jobs and workout previews.

These contracts describe data flow, not permission. Existing owner, Bot policy,
connector scope, and approval checks remain authoritative. Unknown executors
keep their existing lifecycle; no model-supplied contract can grant an action.
"""
import hashlib
import json


class JobContractError(ValueError):
    """A request, route, or result disagrees with the host's job definition."""


def _contract(prefix, text, source, operation, kind, actions, mode):
    return {
        "version": 1,
        "executor": prefix,
        "request_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "source": source,
        "operation": operation,
        "result_kind": kind,
        "allowed_actions": list(actions),
        "mode": mode,
    }


def contract_for_task(executor_prefix, task_text):
    """Derive a contract from a validated host command, never from model fields."""
    prefix = str(executor_prefix or "repo")
    text = str(task_text or "").strip()
    if prefix not in {"level6", "calendar", "gmail"}:
        return None
    import serve  # lazy: importing the task store stays lightweight

    if prefix == "level6":
        definitions = {
            serve.LEVEL6_MESSAGE_PREVIEW_REQUEST: (
                "facebook+glofox", "preview", "workout_preview",
                ("facebook.read", "glofox.read", "preview.render"), "level6_preview"),
            serve.LEVEL6_WEEKLY_REQUEST: (
                "facebook+glofox", "read", "workout_week",
                ("facebook.read", "glofox.read"), "level6_weekly"),
            serve.LEVEL6_CALENDAR_REQUEST: (
                "calendar+glofox", "read", "workout_week",
                ("calendar.read", "glofox.read"), "level6_calendar"),
        }
        definition = definitions.get(text)
        # Delivery and calendar writes retain their existing confirmation gates.
        return _contract(prefix, text, *definition) if definition else None
    if prefix == "calendar":
        if not serve.calendar_task_supported(text):
            raise JobContractError("Calendar job requires a supported read command.")
        return _contract(prefix, text, "calendar", "read", "calendar_agenda",
                         ("calendar.read",), "calendar")
    if serve.canonical_gmail_task(text) != text:
        raise JobContractError("Email job requires a canonical read command.")
    if text.startswith(serve.GMAIL_TASK_READ + " id "):
        mode = "read"
    elif text.startswith(serve.GMAIL_TASK_READ + " "):
        mode = "read_query"
    elif text.startswith(serve.GMAIL_TASK_MESSAGE + " "):
        mode = "message"
    elif text == serve.GMAIL_TASK_LATEST or text.startswith(serve.GMAIL_TASK_LATEST + " "):
        mode = "latest"
    else:
        mode = "search"  # search and continuation share the same result schema
    return _contract(prefix, text, "gmail", "read", "email_" + mode,
                     ("gmail.read",), mode)


def validate_request_route(request_text, executor_prefix, task_text):
    """Compare recognizable user intent with the route BEFORE queuing work.

    Reuse Kyrex's deterministic parsers. A natural email question may be
    refined from search to a full read; an explicit read may not be downgraded.
    A preview always stays a preview and cannot become delivery or a write.
    """
    import serve
    raw = str(request_text or "").strip()
    preview = serve.natural_level6_preview_command(raw)
    if preview:
        expected = contract_for_task("level6", serve.LEVEL6_MESSAGE_PREVIEW_REQUEST)
    else:
        gmail = serve.canonical_gmail_task(raw)
        natural_gmail = None if gmail else serve.natural_gmail_command(raw)
        if gmail or natural_gmail:
            expected = contract_for_task("gmail", gmail or natural_gmail)
        else:
            weekly = {serve.LEVEL6_TASK_TEXT: serve.LEVEL6_WEEKLY_REQUEST,
                      serve.LEVEL6_CALENDAR_TASK_TEXT: serve.LEVEL6_CALENDAR_REQUEST}
            level6 = weekly.get(raw) or (
                serve.LEVEL6_CALENDAR_REQUEST if serve.natural_level6_calendar_command(raw) else None)
            calendar = (raw if serve.calendar_task_supported(raw) else
                        serve.natural_calendar_command(raw))
            if level6:
                expected = contract_for_task("level6", level6)
            elif calendar:
                expected = contract_for_task("calendar", calendar)
            else:
                return  # ambiguous requests still use the existing router
    actual = contract_for_task(executor_prefix, task_text)
    if actual is None or expected["source"] != actual["source"]:
        raise JobContractError("The selected route does not match the requested source.")
    if preview and actual["mode"] != "level6_preview":
        raise JobContractError("A workout preview cannot become a delivery or another job.")
    if not preview and gmail and gmail != str(task_text or "").strip():
        raise JobContractError("The selected email command does not match the explicit request.")
    if (not preview and expected["source"] == "gmail"
            and expected["mode"] in {"read", "read_query", "latest"}
            and actual["mode"] not in {"read", "read_query", "latest"}):
        raise JobContractError("A full email read cannot be replaced by a search or preview.")


def validate_task_contract(task):
    """Check a persisted contract at execution; derive one for pre-migration rows."""
    expected = contract_for_task(task.get("executor_prefix"), task.get("task_text"))
    saved = task.get("job_contract")
    if saved is not None:
        if isinstance(saved, str):
            try:
                saved = json.loads(saved)
            except (ValueError, TypeError):
                raise JobContractError("The saved job contract is unreadable.") from None
        if saved != expected:
            raise JobContractError("The queued job no longer matches its saved contract.")
    return expected


def validate_result(contract, result):
    """Validate structured output before it can be stored or relayed to Chat."""
    if contract is None:
        return
    if not isinstance(result, dict) or result.get("mode") != contract["mode"]:
        raise JobContractError("The job returned a result from the wrong source or operation.")
    if result.get("status") not in {"no_changes", "error", "failed"}:
        raise JobContractError("A read or preview job returned an invalid completion status.")
    if not isinstance(result.get("final_response"), str) or not result["final_response"].strip():
        raise JobContractError("The job returned no readable response.")
    count = result.get("count")
    if type(count) is not int or count < 0:
        raise JobContractError("The job returned an invalid item count.")
    if result.get("status") in {"error", "failed"} or result.get("outcome") == "unavailable":
        if count != 0:
            raise JobContractError("An unavailable read cannot contain successful items.")
        return
    if contract["result_kind"] in {"workout_preview", "workout_week"}:
        lines = result.get("lines")
        if (not isinstance(lines, list) or len(lines) != 6 or count != 6
                or any(not isinstance(line, str) or not line.strip() for line in lines)):
            raise JobContractError("The workout job did not return six workout entries.")
        if contract["result_kind"] == "workout_preview":
            if not result["final_response"].startswith("Preview only — nothing sent."):
                raise JobContractError("The workout job did not return a preview response.")
    elif contract["source"] == "calendar":
        if not isinstance(result.get("window"), str) or not result["window"]:
            raise JobContractError("The calendar read did not identify its window.")
    elif contract["source"] == "gmail":
        if not isinstance(result.get("message_ids"), list) or not isinstance(result.get("query"), str):
            raise JobContractError("The email read did not return its search or selection metadata.")
