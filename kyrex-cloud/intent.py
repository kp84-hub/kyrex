"""intent.py — classify bare (no-prefix) Telegram messages to an executor.

A bare message (no @bot, no 'x:' prefix) is mapped to cal/fs/repo/chat. The
classifier only picks *which* executor; the existing tier/policy/audit gate
still fires on the resolved action. On any failure it returns 'chat' (safe:
answers, never acts). Mirrors the model-call shape in git_workflow.py.

Provider configuration: both :func:`classify_intent` and
:func:`answer_chat` read KYREX_PROVIDER / KYREX_MODEL / KYREX_API_KEY /
OPENAI_BASE_URL (or ANTHROPIC_BASE_URL) from the environment when called
without ``provider_config`` — the regular chat composer's behaviour,
unchanged. A Bot-bound turn passes ``provider_config`` (a
:class:`profiles.ProviderConfig`) and then ONLY the profile's values are
used — the global env is never consulted, never fallen back to.
``KYREX_CUSTOM_HEADERS`` (JSON object) or the profile's approved custom
headers are merged onto each request; hop-by-hop/identity headers are
stripped and can never override the Authorization / x-api-key headers.
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

# Headers a provider profile / KYREX_CUSTOM_HEADERS may never set: they are
# hop-by-hop or identity headers (same list as profiles._HOP_HEADERS).
_HOP_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "host",
    "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "te", "trailer",
})


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


def _resolve_provider(provider_config) -> tuple[str, str, str, str, dict]:
    """Return (provider, model, api_key, base_url, custom_headers).

    With *provider_config* (a profiles.ProviderConfig or any object/dict
    with the same fields) ONLY its values are used — the global env is
    never read.  Without one, the env defaults apply (regular chat).
    """
    if provider_config is not None:
        get = (lambda k: provider_config.get(k)
               if isinstance(provider_config, dict)
               else getattr(provider_config, k, ""))
        provider = (get("provider") or "openai")
        model = get("model") or ""
        api_key = get("api_key") or ""
        if provider == "anthropic":
            base_url = get("base_url") or "https://api.anthropic.com"
        else:
            base_url = get("base_url") or "https://api.openai.com/v1"
        headers = dict(get("headers") or {})
        return provider, model, api_key, base_url, headers
    provider = os.environ.get("KYREX_PROVIDER", "openai")
    model = os.environ.get("KYREX_MODEL")
    api_key = os.environ.get("KYREX_API_KEY")
    if provider == "anthropic":
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    else:
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    env_headers = {}
    raw = os.environ.get("KYREX_CUSTOM_HEADERS", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                env_headers = parsed
        except json.JSONDecodeError:
            pass
    return provider, model or "", api_key or "", base_url, env_headers


def _merge_headers(custom_headers: dict) -> dict:
    """Merge approved custom headers; strip hop-by-hop/identity ones."""
    headers = {"content-type": "application/json"}
    for k, v in (custom_headers or {}).items():
        if str(k).strip().lower() in _HOP_HEADERS:
            continue
        headers[str(k)] = str(v)
    return headers


def _post_llm(provider: str, model: str, api_key: str, base_url: str,
              custom_headers: dict, *, system: str | None, messages: list,
              max_tokens: int) -> str:
    """One provider request; returns the assistant text. Raises on failure.

    Auth/transport headers are set AFTER the approved custom headers so a
    profile can never override Authorization / x-api-key / content-type.
    """
    headers = _merge_headers(custom_headers)
    if provider == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        payload = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system is not None:
            payload["system"] = system
        req = urllib.request.Request(
            f"{base_url}/v1/messages",
            data=json.dumps(payload).encode(),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return "".join(b.get("text", "") for b in data.get("content", []))
    headers["Authorization"] = f"Bearer {api_key}"
    headers["User-Agent"] = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if system is not None:
        payload["messages"] = ([{"role": "system", "content": system}] + messages)
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def classify_intent(text: str, provider_config=None) -> dict:
    """Return {"executor", "instruction", "confidence"}.

    Falls back to a safe 'chat' verdict on any error, bad JSON, or missing model
    config -- so a classifier failure never turns into an unwanted action.
    instruction defaults to the original text; confidence defaults to 0.0.

    With *provider_config* (Bot-bound turn) the classifier runs on the
    Bot's own profile; if that profile cannot supply a model/key the safe
    verdict is returned exactly like a missing env config would be.
    """
    safe = {"executor": "chat", "instruction": text, "confidence": 0.0}
    provider, model, api_key, base_url, custom_headers = _resolve_provider(provider_config)
    if not api_key or not model:
        return safe
    sys = _system_prompt()
    try:
        # Preserve the classifier's original prompt shape: the system
        # prompt is embedded in the user message (no system role).
        reply = _post_llm(
            provider, model, api_key, base_url, custom_headers,
            system=None,
            messages=[{"role": "user", "content": sys + "\n\nMessage: " + text}],
            max_tokens=200,
        )
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


def answer_chat(text: str, history=None, provider_config=None) -> str:
    """Send *text* as a conversational LLM request and return the assistant's
    response as a plain string.

    Reuses the exact same provider/API plumbing as :func:`classify_intent`.
    Called without *provider_config* the environment variables
    (KYREX_PROVIDER, KYREX_MODEL, KYREX_API_KEY, OPENAI_BASE_URL /
    ANTHROPIC_BASE_URL) apply — the regular chat composer's behaviour,
    unchanged.  A Bot-bound turn passes ``provider_config`` (the Bot's
    resolved profile) and then only that profile's provider, base URL,
    API key, approved headers, and exact model are used — never the
    global env.  On any error (missing config, network failure, API
    error, bad response) a short user-facing message is returned instead
    of raising — the caller (typically a Telegram handler) can send that
    directly without further error handling.

    The returned string is bounded to ~4000 characters so it fits comfortably
    inside Telegram's 4096-character message limit.  If the response is longer
    the tail is cut at a sentence boundary and a truncation notice is appended.
    """
    provider, model, api_key, base_url, custom_headers = _resolve_provider(provider_config)
    if not api_key or not model:
        return ("I can check your calendar, read files, or take a repo task. "
                "Prefix with cal:, fs:, or repo: to be explicit.")

    try:
        reply = _post_llm(
            provider, model, api_key, base_url, custom_headers,
            system=_CHAT_SYSTEM_PROMPT,
            messages=(
                list(history or [])
                + [{"role": "user", "content": text}]
            ),
            max_tokens=500,
        )

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
