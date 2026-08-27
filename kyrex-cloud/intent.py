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

_ALLOWED = {"cal", "fs", "repo", "chat"}
_PROMPT_PATH = Path(__file__).parent / "intent_prompt.txt"


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
                        {"role": "system", "content": sys},
                        {"role": "user", "content": text},
                    ],
                    "max_tokens": 200,
                }).encode(),
                method="POST",
                headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            reply = data["choices"][0]["message"]["content"]
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
    except Exception:
        return safe
