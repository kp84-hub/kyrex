"""chat_service.py — Kyrex Chat engine integration + persistence.

This is the standalone Chat product's backend core. It answers conversational
prompts by driving the *existing* Kyrex engine/provider layer directly, and
persists conversations as JSON files under the existing Cloud data root
(``kyrex-cloud/paths.py`` ``data_dir()`` — the same ``KYREX_DATA_DIR`` used by
bots/audit/MCP).

Isolation invariants (deliberately preserved):
  * No ~/.px/config.json global fallback. Provider/model/key resolution uses
    the exact environment keys the Cloud already uses for every other path
    (``KYREX_PROVIDER``, ``KYREX_MODEL``, ``KYREX_API_KEY``,
    ``OPENAI_BASE_URL`` / ``ANTHROPIC_BASE_URL``). There is no second
    ConfigManager and no silent global-config read.
  * Conversations are keyed per-user and stored under a per-user directory, so
    Chat can never see another bot/workspace/IDE session's state.
  * The engine is invoked with ``tools=None``: a pure conversational
    completion. This is a *chat*, not an agent task, so it does not (and must
    not) traverse the tier/policy/approval/audit gate that serves agent tasks.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional

# ── paths ──────────────────────────────────────────────────────────
# Resolve the shared data root exactly as the rest of Kyrex Cloud does.
SCRIPT_DIR = Path(__file__).resolve().parent            # web/backend/
KYREX_CLOUD_DIR = SCRIPT_DIR.parent.parent              # kyrex-cloud/
sys.path.insert(0, str(KYREX_CLOUD_DIR))

from paths import data_dir as _data_dir  # noqa: E402

# ── engine import ──────────────────────────────────────────────────
# The Kyrex engine lives in the sibling ``kyrex_engine/`` package. Import its
# provider factory and config manager so we reuse the real provider plumbing
# (retry/backoff, streaming callbacks) instead of re-implementing it.
ENGINE_DIR = KYREX_CLOUD_DIR.parent / "kyrex_engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from kyrex.providers import get_provider  # noqa: E402

# ── config ─────────────────────────────────────────────────────────
CHAT_DIR_NAME = "chat"
CHAT_SYSTEM_PROMPT = (
    "You are Kyrex Chat, the conversational assistant product from Kyrex. "
    "Answer clearly, directly, and in a natural conversational tone. "
    "Format responses with Markdown where it aids readability."
)

MAX_MESSAGE_CHARS = 32_000


class ChatUnavailable(Exception):
    """Raised when the engine/provider cannot be reached or is unconfigured."""


def _chat_root() -> Path:
    root = _data_dir() / CHAT_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _user_dir(user: str) -> Path:
    """Per-user subdirectory guarantees cross-user isolation."""
    if not user:
        raise ValueError("user is required")
    # Sanitize to a filesystem-safe segment.
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in user)
    d = _chat_root() / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _conv_path(user: str, conversation_id: str) -> Path:
    return _user_dir(user) / f"{conversation_id}.json"


# ── model / provider resolution (existing engine env keys) ────────

def _resolve_provider() -> dict:
    """Return {provider, model, api_key, base_url} from the existing env keys.

    Uses the same keys every other Cloud model call uses; no config-file
    fallback. ``provider``/``base_url`` are resolved the same way
    ``kyrex.providers.get_provider`` does.
    """
    provider = (os.environ.get("KYREX_PROVIDER") or os.environ.get("PROVIDER") or "openai").lower()
    model = os.environ.get("KYREX_MODEL") or ""
    api_key = os.environ.get("KYREX_API_KEY") or ""
    if provider == "anthropic":
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    else:
        base_url = os.environ.get("KYREX_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
    return {"provider": provider, "model": model.strip(), "api_key": api_key.strip(), "base_url": base_url.strip()}


def engine_available() -> tuple[bool, str]:
    cfg = _resolve_provider()
    if not cfg["model"]:
        return False, "KYREX_MODEL is not configured"
    if not cfg["api_key"]:
        return False, "KYREX_API_KEY is not configured"
    return True, f"{cfg['provider']}/{cfg['model']}"


# ── persistence ────────────────────────────────────────────────────

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def create_conversation(user: str, title: str = "New chat") -> dict:
    now = _now_iso()
    conv = {
        "conversation_id": uuid.uuid4().hex,
        "title": title or "New chat",
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    _write(user, conv)
    return conv


def _write(user: str, conv: dict) -> None:
    conv["updated_at"] = _now_iso()
    path = _conv_path(user, conv["conversation_id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(conv, indent=2))
    tmp.replace(path)


def get_conversation(user: str, conversation_id: str) -> Optional[dict]:
    path = _conv_path(user, conversation_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data


def list_conversations(user: str) -> list[dict]:
    d = _user_dir(user)
    out = []
    for p in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "conversation_id": data.get("conversation_id", p.stem),
            "title": data.get("title", "New chat"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "message_count": len(data.get("messages", [])),
        })
    return out


def delete_conversation(user: str, conversation_id: str) -> bool:
    path = _conv_path(user, conversation_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def _append_message(user: str, conv: dict, role: str, content: str) -> dict:
    msg = {
        "id": uuid.uuid4().hex,
        "role": role,
        "content": content,
        "created_at": _now_iso(),
    }
    conv["messages"].append(msg)
    return msg


def _title_from(user_message: str) -> str:
    t = " ".join(user_message.split())
    return t[:40] + ("..." if len(t) > 40 else "") or "New chat"


# ── engine invocation (streaming) ──────────────────────────────────

def build_messages(history: list[dict], user_content: str) -> list[dict]:
    """Assemble the provider message list, mirroring the existing chat path:
    a leading system prompt, prior turns, then the new user turn."""
    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
    for m in history or []:
        role = m.get("role")
        if role not in ("user", "assistant", "system"):
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_content})
    return messages


# Sentinel payload markers placed on the worker->loop queue so the streaming
# coroutine can distinguish terminal outcomes (completion / provider error)
# from incrementally streamed token deltas.
_SENTINEL = object()
_ERROR = object()
_CANCELLED = object()


def _provider_error_content(content: str) -> bool:
    """Detect the provider's swallowed-error shape.

    Both OpenAIProvider and AnthropicProvider catch every exception inside
    their streaming path and return ``content`` prefixed with
    ``[<name> Provider Error: ...`` instead of raising. This is the only signal
    the Chat service receives for a mid-stream or pre-first-token failure, so
    we detect it here and convert it into a deterministic error event rather
    than persisting it as a successful assistant message.
    """
    if not content:
        return False
    low = content.lower()
    return ("provider error:" in low) or (content.lstrip().startswith("[") and "error:" in low)


async def stream_chat(
    user: str,
    conversation_id: str,
    user_content: str,
    cancel_event: Optional[asyncio.Event] = None,
) -> AsyncIterator[dict]:
    """Stream assistant output for one user turn.

    Yields control frames (``dict``) rather than raw strings:
      * ``{"type": "conversation", "conversation_id": ...}`` — once, first.
      * ``{"type": "delta", "content": <token>}`` — each incremental token.
    Termination is *not* yielded from here; the terminal frame is produced by
    the caller after this generator returns a ``(status, final_text)`` pair:

      ``status`` is one of ``"complete"``, ``"error"``, ``"cancelled"``.

    The provider's ``stream_callback`` is bridged to the event loop via a
    stdlib ``queue.Queue`` (thread-safe) — never ``asyncio.Queue`` across
    threads — drained by this coroutine. The worker thread is always joined
    before returning so no orphaned provider call outlives the request.
    """
    ok, detail = engine_available()
    if not ok:
        raise ChatUnavailable(detail)

    cfg = _resolve_provider()
    provider = get_provider(cfg["provider"], cfg["api_key"], base_url=cfg["base_url"] or None)

    conv = get_conversation(user, conversation_id)
    if conv is None:
        conv = create_conversation(user, title=_title_from(user_content))
        conversation_id = conv["conversation_id"]

    history = conv.get("messages", [])
    messages = build_messages(history, user_content)

    # Persist the user message up-front so a concurrent read sees the turn.
    _append_message(user, conv, "user", user_content)
    _write(user, conv)

    # Thread-safe bridge: the engine's provider call is blocking/async and runs
    # in a worker thread with its own event loop. We use a stdlib queue.Queue
    # (thread-safe, no cross-thread asyncio.Queue). The provider's own
    # ``interrupt_event`` is used for cooperative cancellation: an
    # ``asyncio.Event`` that lives in the *worker thread's* loop, set from here
    # via ``call_soon_threadsafe``.
    import queue as _queue
    q: _queue.Queue = _queue.Queue()
    cancel = cancel_event if cancel_event is not None else asyncio.Event()

    # Provider-facing interrupt event (created in the worker loop, set from the
    # event-loop side). A list-of-one is used as a mutable handle into the
    # worker thread since it must be created inside that same loop.
    interrupt_handle: list = [None]

    def _on_token(text: str) -> None:
        if text:
            q.put(text)

    def _run_blocking() -> None:
        outcome = _SENTINEL  # default: clean completion (no error)
        try:
            # The provider's chat() is async; run it in this thread's own loop.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                interrupt = asyncio.Event()
                interrupt_handle[0] = (loop, interrupt)
                result = loop.run_until_complete(provider.chat(
                    model=cfg["model"],
                    messages=messages,
                    tools=None,
                    stream_callback=_on_token,
                    interrupt_event=interrupt,
                ))
                # The provider swallows exceptions into error-prefixed content.
                content = (result or {}).get("content") or ""
                if _provider_error_content(content):
                    outcome = _ERROR
                    q.put({"__error__": content})
                else:
                    outcome = _SENTINEL
            finally:
                interrupt_handle[0] = None
                loop.close()
        except Exception as exc:  # an exception that escaped the provider layer
            outcome = _ERROR
            q.put({"__error__": str(exc)})
        finally:
            # Always signal termination exactly once, carrying the outcome so
            # the drainer knows whether this was a clean finish or a failure.
            q.put({"__outcome__": outcome})

    worker = threading.Thread(target=_run_blocking, daemon=True,
                              name=f"chat-{conversation_id[:12]}")
    worker.start()

    def _request_cancel() -> None:
        """Cooperatively stop the in-flight provider call.

        Sets the provider-facing asyncio.Event from the calling thread; the
        provider checks it per chunk and stops producing tokens. This is the
        safest available behavior: it cannot abort an in-flight HTTP request
        mid-flight (the engine has no lower-level transport cancellation), but
        it stops token delivery immediately and lets the worker unwind.
        """
        h = interrupt_handle[0]
        if h is not None:
            loop, interrupt = h
            loop.call_soon_threadsafe(interrupt.set)

    full: list[str] = []
    result = None
    first = True
    outcome = _SENTINEL
    # Poll cadence for observing cancellation while the provider is quiet
    # between tokens. Kept short so a cancel or client disconnect is noticed
    # promptly even if the provider stalls mid-stream.
    POLL_SECONDS = 0.05
    try:
        while True:
            # Wait briefly for the next frame WITHOUT blocking the event loop:
            # q.get() is a blocking stdlib call, so it must never run directly
            # on the loop thread. Bridge it through asyncio.to_thread (worker
            # thread) and await the result — the loop stays fully responsive
            # during generation. The short timeout still lets us observe
            # cancellation during provider silence without busy-spinning.
            try:
                token = await asyncio.to_thread(q.get, True, POLL_SECONDS)
            except _queue.Empty:
                if cancel.is_set():
                    outcome = _CANCELLED
                    _request_cancel()
                    break
                continue
            if isinstance(token, dict) and "__outcome__" in token:
                outcome = token["__outcome__"]
                break
            if isinstance(token, dict) and "__error__" in token:
                outcome = _ERROR
                result = token["__error__"]
                break
            if cancel.is_set():
                outcome = _CANCELLED
                _request_cancel()
                break
            full.append(token)
            if first:
                yield {"type": "conversation", "conversation_id": conversation_id}
                first = False
            yield {"type": "delta", "content": token}
    finally:
        # A cancelled or disconnected stream must not leave the worker running.
        if cancel.is_set():
            _request_cancel()
        # Joining the worker blocks; never do it on the event-loop thread, or
        # provider unwind time (up to the 30s cap) starves every other request.
        # Off-load to a worker thread instead. If finalization happens without
        # a running loop (post-close GC), fall back to a direct join — there is
        # no loop left to starve in that case.
        try:
            await asyncio.to_thread(worker.join, 30.0)
        except RuntimeError:
            worker.join(timeout=30.0)
        # Drain any residual frames the worker may have enqueued so the queue
        # and worker's producer never deadlock on a full/non-consumed queue.
        # A terminal outcome decided before this point (cancel/error) is
        # authoritative and must not be overwritten by a late clean completion.
        while True:
            try:
                leftover = q.get_nowait()
            except _queue.Empty:
                break
            if isinstance(leftover, dict) and "__outcome__" in leftover:
                if outcome in (_SENTINEL,):
                    outcome = leftover["__outcome__"]
            elif isinstance(leftover, dict) and "__error__" in leftover:
                result = leftover["__error__"]
                if outcome not in (_CANCELLED,):
                    outcome = _ERROR

    final_text = "".join(full).strip()

    # Persistence: only a successfully-completed turn persists an assistant
    # message. Failed and cancelled streams are never recorded as a completed
    # assistant reply (no duplicate/false assistant messages).
    if outcome is _SENTINEL and final_text:
        conv_now = get_conversation(user, conversation_id) or conv
        _append_message(user, conv_now, "assistant", final_text)
        _write(user, conv_now)

    # Terminal status frame. An async generator cannot ``return`` a value, so
    # the terminal outcome is yielded as the final control frame, which the
    # caller (_drive_stream) maps to the matching explicit SSE event.
    if outcome is _ERROR:
        yield {"type": "status", "status": "error",
               "message": result or "provider error"}
    elif outcome is _CANCELLED:
        yield {"type": "status", "status": "cancelled", "content": final_text}
    else:
        yield {"type": "status", "status": "complete", "content": final_text}
