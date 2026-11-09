"""Recognize explicit repository work and scoped Developer Bot follow-ups."""
import re


_PR = r"(?:pr|pull\s+request)"
_BOT = r"(?:dev(?:eloper)?\s+bot)"
_REQUEST = r"(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
_DELEGATE = r"(?:have|ask|get|tell)\s+(?:the\s+)?" + _BOT + r"\s+(?:to\s+)?"
_OPEN_PR = (r"\b(?:open|create|prepare|publish|submit|raise)\s+"
            r"(?:(?:a|the|this|that)\s+)?(?:github\s+)?" + _PR + r"\b")
_PREPARE = re.compile(
    r"^" + _REQUEST + r"(?:" + _DELEGATE + r")?"
    r"(?:" + _OPEN_PR + r"|(?:carry|port|move|bring|apply|cherry-pick|rebase|take|fix|update|implement|run)\b"
    r"[^?]{0,1000}?(?:[,;]\s*|\s+)(?:and\s+|then\s+|and\s+then\s+)" + _OPEN_PR + r")", re.I,
)
_PR_STATUS = re.compile(
    r"(?:"
    + _PR + r"\s+status|(?:any\s+update|status)\s+on\s+(?:the\s+)?" + _PR
    + r"|(?:what(?:'s|\s+is)\s+the\s+status\s+of|check\s+on)\s+(?:the\s+)?" + _PR
    + r"|(?:is|has)\s+(?:the\s+)?" + _PR + r"\s+(?:been\s+)?(?:open(?:ed)?|ready|published)(?:\s+yet)?"
    + r"|did\s+(?:the\s+)?(?:" + _BOT + r"|it|you)\s+(?:open|publish|create)\s+(?:the\s+)?" + _PR + r"(?:\s+yet)?"
    + r")[.?!]?", re.I,
)
_DEV_STATUS = re.compile(
    r"(?:what(?:'s|\s+is)\s+(?:the\s+)?" + _BOT + r"\s+doing"
    r"|(?:is|did)\s+(?:the\s+)?" + _BOT + r"\s+(?:still\s+)?(?:running|working|done|finish)"
    r"|(?:any\s+update|status)\s+on\s+(?:the\s+)?" + _BOT + r")[.?!]?", re.I,
)
_WORK = re.compile(
    r"^" + _REQUEST + r"(?P<delegated>" + _DELEGATE + r"|" + _BOT + r"[:,]\s*)?"
    r"(?P<verb>debug|diagnose|investigate|trace|reproduce|fix|patch|implement|refactor|"
    r"review|inspect|check|test|run|build|write|add|change|update|remove)\s+(?P<subject>.+)", re.I,
)
_CODE = re.compile(
    r"\b(?:code(?:base)?|repository|repo|implementation|bug|regression|parser|source files?|"
    r"frontend|backend|handler|executor|routing|stack trace|diff|branch|commit|test suite|"
    r"(?:unit|integration|regression) tests?|pytest|npm test|CI (?:check|failure))\b"
    r"|\b[\w.-]+\.(?:py|go|js|jsx|ts|tsx|rs|kt|swift)\b", re.I,
)
_UI_CHANGE = re.compile(r"\b(?:button|toggle|dialog|panel|screen|UI)\b", re.I)
_UI_DESTINATION = re.compile(r"\b(?:to|in|on)\s+[^.!?]*\b(?:panel|screen|UI|page|toolbar|sidebar)\b", re.I)
_REPO_DESTINATION = re.compile(
    r"\b(?:to|in|from)\s+(?:(?:the|this|our)\s+)?(?:repo(?:sitory)?|codebase|frontend|backend)\b", re.I,
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


def _explicit_action(text):
    if _PR_STATUS.fullmatch(text):
        return "pr_status"
    if _DEV_STATUS.fullmatch(text):
        return "status"
    if _PREPARE.match(text):
        return "prepare"
    work = _WORK.match(text)
    if work:
        # Quoted text can be an event title or a search query. It is not
        # evidence that the owner asked for a repository operation.
        subject = re.sub(r'"[^"]*"|“[^”]*”', '', work['subject']).strip()
        verb = work['verb'].casefold()
        target = re.split(r'\s+(?:for|about|containing|mentioning|titled|named|called)\s+', subject,
                          maxsplit=1, flags=re.I)[0]
        named_dev_task = work['delegated'] and verb in {
            'debug', 'diagnose', 'investigate', 'trace', 'reproduce', 'fix', 'review', 'inspect', 'test',
        }
        if (named_dev_task or verb in {'implement', 'refactor', 'patch'}
                or (verb != 'add' and _CODE.search(target))
                or _REPO_DESTINATION.search(subject)
                or (verb == 'run' and re.fullmatch(r'(?:the |all )?(?:tests|full suite)[.?!]?', subject, re.I))
                or (verb == 'add' and _UI_CHANGE.search(target) and _UI_DESTINATION.search(subject))
                or (verb in {'build', 'change', 'update', 'remove'} and _UI_CHANGE.search(target))):
            return "work"
    return None


def action(text, messages=()):
    text = _normalized(text)
    explicit = _explicit_action(text)
    if explicit:
        return explicit
    if _SHORT_STATUS.fullmatch(text):
        for message in reversed(messages):
            if message.get('role') != 'user':
                continue
            prior = _normalized(message.get('content'))
            if _SHORT_STATUS.fullmatch(prior):
                continue
            previous = _explicit_action(prior)
            if previous:
                return "pr_status" if previous in {"prepare", "pr_status"} else "status"
            break
    return None


def task_status(status, name="Developer Bot", progress="", *, preparing_pr=False):
    work = "PR preparation" if preparing_pr else "repository task"
    if status == "queued":
        return f"{name} is queued for {work}."
    if status == "running":
        return f"{name} is working on the {work}." + (f" {progress}" if progress else "")
    if status == "awaiting_approval":
        return f"{name} is waiting for your approval. Open Delegated work to review it."
    if status == "done":
        result = f"{name} finished the {work}. Open its chat for the results."
        return f"{result} {REVIEW_STEP}" if preparing_pr else result
    if status == "cancelled":
        return f"{name}’s {work} was cancelled."
    return f"{name} couldn’t finish the {work}. Open the task details for the reason."
