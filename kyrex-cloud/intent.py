"""intent.py — classify bare (no-prefix) Telegram messages to an executor.

A bare message (no @bot, no 'x:' prefix) is mapped to cal/fs/repo/chat. The
classifier only picks *which* executor; the existing tier/policy/audit gate
still fires on the resolved action. On any failure it returns 'chat' (safe:
answers, never acts). Mirrors the model-call shape in git_workflow.py.
"""
import json
import os
import urllib.request
from pathlib import Path

# Telegram message length ceiling (characters).  Messages beyond this are
# truncated with a notice appended.  Telegram's actual limit is 4096, but we
# stay comfortably under to leave room for emoji and formatting overhead.
_TG_MAX = 4000

_ALLOWED = {"cal", "fs", "repo", "chat"}
_PROMPT_PATH = Path(__file__).parent / "intent_prompt.txt"
_CHAT_SYSTEM_PROMPT = (
    "You are a helpful assistant. Answer the user's question conversationally, "
    "clearly, and concisely. Do not mention that you are an AI unless asked. "
    "Keep your response practical and to the point."
)


def _system_prompt() -> str:
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return ("Route to one of cal/fs/repo/chat. Reply ONLY JSON: "
                '{"executor": "chat", "instruction": "", "confidence": 0.0}')


def _extract_json(raw: str) -> dict:
    """Parse the first JSON object out of a model reply, tolerating fences."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in model reply")
    return json.loads(s[start:end + 1])


def classify_intent(text: str) -> dict:
    """Return {"executor", "instruction", "confidence"}.

    Falls back to a safe 'chat' verdict on any error, bad JSON, or missing model
    config -- so a classifier failure never turns into an unwanted action.
    instruction defaults to the original text; confidence defaults to 0.0.
    """
    safe = {"executor": "chat", "instruction": text, "confidence": 0.0}
    provider = os.environ.get("KYREX_PROVIDER", "openai")
    model = os.environ.get("KYREX_MODEL")
    api_key = os.environ.get("KYREX_API_KEY")
    if not api_key or not model:
        return safe
    sys = _system_prompt()
    try:
        if provider == "anthropic":
            base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
            req = urllib.request.Request(
                f"{base_url}/v1/messages",
                data=json.dumps({
                    "model": model, "max_tokens": 200,
                    "system": sys,
                    "messages": [{"role": "user", "content": text}],
                }).encode(),
                method="POST",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            reply = "".join(b.get("text", "") for b in data.get("content", []))
        else:
            base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            req = urllib.request.Request(
                f"{base_url.rstrip('/')}/chat/completions",
                data=json.dumps({
                    "model": model,
                    "messages": [
                        {"role": "user", "content": sys + "\n\nMessage: " + text},
                    ],
                    "max_tokens": 200,
                }).encode(),
                method="POST",
                headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            reply = data["choices"][0]["message"]["content"]
        import sys as _sys2
        print(f'[intent] raw reply: {reply[:300]!r}', file=_sys2.stderr)
        verdict = _extract_json(reply)
        exec = str(verdict.get("executor", "chat")).strip().lower()
        if exec not in _ALLOWED:
            return safe
        try:
            conf = float(verdict.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        instr = str(verdict.get("instruction") or text).strip()
        return {"executor": exec, "instruction": instr, "confidence": conf}
    except Exception as _e:
        import sys as _sys
        _body = ''
        if hasattr(_e, 'read'):
            try: _body = _e.read().decode()[:400]
            except Exception: pass
        print(f'[intent] classify failed: {type(_e).__name__}: {_e} | body={_body!r}', file=_sys.stderr)
        return safe


def answer_chat(text: str) -> str:
    """Send *text* as a conversational LLM request and return the assistant's
    response as a plain string.

    Reuses the exact same provider/API plumbing as :func:`classify_intent`
    (KYREX_PROVIDER, KYREX_MODEL, KYREX_API_KEY, OPENAI_BASE_URL /
    ANTHROPIC_BASE_URL).  On any error (missing config, network failure, API
    error, bad response) a short user-facing message is returned instead of
    raising — the caller (typically a Telegram handler) can send that directly
    without further error handling.

    The returned string is bounded to ~4000 characters so it fits comfortably
    inside Telegram's 4096-character message limit.  If the response is longer
    the tail is cut at a sentence boundary and a truncation notice is appended.
    """
    provider = os.environ.get("KYREX_PROVIDER", "openai")
    model = os.environ.get("KYREX_MODEL")
    api_key = os.environ.get("KYREX_API_KEY")
    if not api_key or not model:
        return ("I can check your calendar, read files, or take a repo task. "
                "Prefix with cal:, fs:, or repo: to be explicit.")

    try:
        if provider == "anthropic":
            base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
            req = urllib.request.Request(
                f"{base_url}/v1/messages",
                data=json.dumps({
                    "model": model, "max_tokens": 500,
                    "system": _CHAT_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": text}],
                }).encode(),
                method="POST",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            reply = "".join(b.get("text", "") for b in data.get("content", []))
        else:
            base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            req = urllib.request.Request(
                f"{base_url.rstrip('/')}/chat/completions",
                data=json.dumps({
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _CHAT_SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                    "max_tokens": 500,
                }).encode(),
                method="POST",
                headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json",
                         "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            reply = data["choices"][0]["message"]["content"]

        reply = (reply or "").strip()
        if not reply:
            reply = "I'm not sure how to answer that. Could you rephrase?"

        # Bound to Telegram-friendly length, cutting at the last sentence
        # boundary that fits.
        if len(reply) > _TG_MAX:
            cutoff = reply.rfind(". ", 0, _TG_MAX - 20)
            if cutoff == -1:
                cutoff = reply.rfind(" ", 0, _TG_MAX - 20)
            if cutoff == -1:
                cutoff = _TG_MAX - 20
            reply = reply[:cutoff + 1].rstrip(". ") + "...  (response truncated)"
        return reply

    except Exception as _e:
        import sys as _sys
        _body = ""
        if hasattr(_e, "read"):
            try:
                _body = _e.read().decode()[:400]
            except Exception:
                pass
        print(f"[intent] answer_chat failed: {type(_e).__name__}: {_e} | body={_body!r}",
              file=_sys.stderr)
        return ("I can check your calendar, read files, or take a repo task. "
                "Prefix with cal:, fs:, or repo: to be explicit.")
