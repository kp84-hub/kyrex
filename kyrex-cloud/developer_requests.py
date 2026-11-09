"""Bounded owner requests for preparing and following a pull request."""
import re


_PR = r"(?:pr|pull\s+request)"
_BOT = r"(?:dev(?:eloper)?\s+bot)"
_OPEN_PR = (r"\b(?:open|create|prepare|publish|submit|raise)\s+"
            r"(?:(?:a|the|this|that)\s+)?(?:github\s+)?" + _PR + r"\b")
_PREPARE = re.compile(
    r"^(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
    r"(?:(?:have|ask|get|tell)\s+(?:the\s+)?" + _BOT + r"\s+(?:to\s+)?)?"
    r"(?:" + _OPEN_PR + r"|(?:carry|port|move|bring|apply|cherry-pick|rebase|take|fix|update|implement|run)\b"
    r"[^?]{0,1000}?(?:[,;]\s*|\s+)(?:and\s+|then\s+|and\s+then\s+)" + _OPEN_PR + r")", re.I,
)
_STATUS = re.compile(
    r"(?:"
    + _PR + r"\s+status|(?:any\s+update|status)\s+on\s+(?:the\s+)?" + _PR
    + r"|(?:what(?:'s|\s+is)\s+the\s+status\s+of|check\s+on)\s+(?:the\s+)?" + _PR
    + r"|(?:is|has)\s+(?:the\s+)?" + _PR + r"\s+(?:been\s+)?(?:open(?:ed)?|ready|published)(?:\s+yet)?"
    + r"|did\s+(?:the\s+)?(?:" + _BOT + r"|it|you)\s+(?:open|publish|create)\s+(?:the\s+)?" + _PR + r"(?:\s+yet)?"
    + r")[.?!]?", re.I,
)
_SHORT_STATUS = re.compile(
    r"(?:any\s+updates?|how(?:'s|\s+is)\s+it\s+going|still\s+(?:running|working)|"
    r"(?:is\s+it\s+)?(?:done|ready)\s+yet|did\s+it\s+finish)[.?!]?", re.I,
)

PREPARE_FOR_REVIEW = (
    "Prepare the requested changes against the repository's current default branch. "
    "Preserve existing work and report any checkout conflict and the tests actually run. "
    "Leave the changes ready for the owner's Repository review. "
    "Do not push, merge, or publish a pull request from this task."
)
REVIEW_STEP = (
    "If the changes are ready, pause Developer Bot and use Repository → Review changes → Create pull request."
)


def _normalized(text):
    return " ".join(str(text or "").replace("’", "'").split())


def pr_action(text, messages=()):
    text = _normalized(text)
    if _STATUS.fullmatch(text):
        return "status"
    if _PREPARE.match(text):
        return "prepare"
    if _SHORT_STATUS.fullmatch(text):
        for message in reversed(messages):
            if message.get('role') != 'user':
                continue
            prior = _normalized(message.get('content'))
            if _SHORT_STATUS.fullmatch(prior):
                continue
            if _STATUS.fullmatch(prior) or _PREPARE.match(prior):
                return "status"
            break
    return None


def pr_status(status, name="Developer Bot", progress=""):
    if status == "queued":
        return f"{name} is queued to prepare the PR for your review."
    if status == "running":
        return f"{name} is preparing the PR." + (f" {progress}" if progress else "")
    if status == "awaiting_approval":
        return f"{name} is waiting for your approval. Open Delegated work to review it."
    if status == "done":
        return f"{name} finished the preparation task. Check its chat for the test results. {REVIEW_STEP}"
    if status == "cancelled":
        return f"{name}’s PR preparation was cancelled."
    return f"{name} couldn’t finish preparing the PR. Open the task details for the reason."
