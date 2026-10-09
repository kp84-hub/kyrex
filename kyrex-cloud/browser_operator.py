#!/usr/bin/env python3
"""browser_operator.py — Kyrex Cloud Browser Operator executor.

Structured, server-side browser automation for a Bot. Speaks the SAME
executor protocol as ``fs_executor.py`` / ``cal_executor.py``:

    stdout  KYREX_PROGRESS:{json}
            KYREX_OPERATION:{json}      (op, target, summary[, detail])
            KYREX_APPROVAL:{json}       (tier, summary[, detail, token])
            KYREX_RESULT_JSON:{json}    (exactly one, at the end)
    stdin   one verdict per KYREX_OPERATION:  ALLOW | APPROVE | DENY
            one decision per KYREX_APPROVAL:  APPROVED | DENIED

The host (``serve.run_task``) owns the tier table and the policy decision.
The executor only *performs* the operation the host permits and, when the
host asks (``APPROVE``), blocks on stdin until a human decision arrives.
That handshake IS the approval pause/resume: the host parks in
``evt.wait()`` while this process parks in ``readline()``. A timeout, a
cancel, or a denial writes ``DENIED`` and this process stops cleanly with a
result — never a stuck workflow.

Structured actions — no arbitrary commands, no arbitrary filesystem paths::

    navigate, read, click, type, upload, download, screenshot, delete

Security boundaries enforced HERE (server-side, per run):

  * Per-Bot site/domain allowlist (``KYREX_BROWSER_ALLOWLIST``). Checked on
    every navigation and download, and re-checked against the page's live
    origin before every subsequent action. An empty allowlist denies all.
  * Sessions are isolated by Bot and owner
    (``<root>/browser-sessions/bot-<id>/owner-<owner>``) and by a per-Bot
    managed CDP endpoint (``KYREX_BROWSER_WS_ENDPOINTS_JSON``). When the host
    manages a persistent session it supplies ``KYREX_BROWSER_SESSION_DIR`` —
    that directory (never a client value) becomes the persistent profile, so
    a run reconnects to the same isolated browser instead of starting cold.
    ``KYREX_BROWSER_MANAGED`` (or the mere presence of that directory) selects
    managed mode, which REUSES the persistent context/profile — never a
    throwaway ``new_context()`` — so a login survives across runs.
  * Upload / download / screenshot paths are confined to the Bot's workspace
    (``KYREX_FS_ROOT``, i.e. the Rift). Symlinks and ``..`` escapes are
    rejected.
  * Credentials, cookies, tokens, authorization headers and any env secret
    are scrubbed from every result, progress note, log line, artifact name
    and approval prompt. Screenshots never embed page text.
  * Unknown actions, non-http(s) URLs, URLs carrying userinfo, and any path
    that escapes the workspace are rejected before execution.

The driver is selected by ``KYREX_BROWSER_DRIVER``: ``playwright`` (default,
real Chromium) or ``local`` (deterministic, dependency-free — used by the
smoke test and offline environments). No other value is accepted: the driver
is never client-selectable and no client-supplied code is ever executed.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import html
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# Feed reads include per-post caption, observed link and image OCR. Preserve
# that evidence through the operator instead of dropping later articles.
MAX_TEXT = 12000
DEFAULT_ROOT = "/tmp/kyrex-browser"

# ── Level 6 weekly: the ONE fixed-purpose host operation ───────────────
# Beyond the generic action vocabulary, this single fixed-purpose operation
# accepts NO caller-controlled URL and NO caller-controlled date. The page is
# pinned HERE; ``parse_spec`` refuses any spec whose ``url`` is not exactly
# this value, and the operation never reads a URL from the spec at run time
# (it always navigates to this constant). It announces ONLY the three
# read-only operations the dedicated Level 6 Weekly policy grants —
# ``browser.navigate`` / ``browser.read`` / ``browser.screenshot`` — so it
# exposes no click/type/submit/delete capability, and every scroll it performs
# is INTERNAL to the operation (never a generic action).
LEVEL6_WEEKLY_ACTION = "level6_weekly"
LEVEL6_PAGE_URL = "https://www.facebook.com/level6training/photos"
LEVEL6_POST_MARKER = "THE WEEKLY SIX"
GOOGLE_MESSAGES_LEVEL6_ACTION = "google_messages_level6"
GOOGLE_MESSAGES_BASE_URL = "https://messages.google.com/web/"
GOOGLE_MESSAGES_DELIVERY_TEST = "Kyrex delivery test — no workout update."

# Actions that navigate somewhere, and therefore require an allowlist check.
_URL_ACTIONS = ("navigate", "download", LEVEL6_WEEKLY_ACTION,
                GOOGLE_MESSAGES_LEVEL6_ACTION)

VALID_ACTIONS = frozenset(
    {"navigate", "read", "click", "type", "upload", "download", "screenshot",
     "delete", LEVEL6_WEEKLY_ACTION, GOOGLE_MESSAGES_LEVEL6_ACTION}
)

# Executor-side tier hints. The host derives the tier it acts on from its own
# OPERATION_TIERS table and only uses these as a raise-only hint; they exist so
# the legacy KYREX_APPROVAL line carries a defensible tier/token.
OP_TIERS: dict[str, int] = {
    "browser.navigate": 0,
    "browser.read": 0,
    "browser.click": 0,
    "browser.screenshot": 0,
    "browser.type": 1,
    "browser.upload": 1,
    "browser.download": 1,
    "browser.submit": 2,
    "browser.delete": 2,
    "messages.send_level6": 0,
}

# Verbs that make a click/typed control consequential. A click that trips one
# of these is announced as browser.submit (T2) rather than browser.click (T0).
CONSEQUENTIAL_VERBS = frozenset(
    {
        "submit", "send", "purchase", "buy", "pay", "checkout", "order",
        "delete", "remove", "trash", "confirm", "publish", "post", "transfer",
    }
)

_SENSITIVE_KEY_RE = re.compile(
    r"(authorization|proxy-authorization|cookie|set-cookie|token|secret|"
    r"password|passwd|api[_-]?key|session|credential)",
    re.IGNORECASE,
)
_SECRET_ENV_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|COOKIE|AUTHORIZATION|CREDENTIAL)",
    re.IGNORECASE,
)
_HEADER_REDACTIONS = (
    re.compile(r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*:\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{4,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|password|"
        r"passwd|secret|session(?:_id)?)\b\s*[:=]\s*[^\s,;]+"
    ),
)


class SpecError(Exception):
    """The client-supplied browser task is not a valid action spec."""


class DriverError(Exception):
    """The requested browser transport is unavailable or unusable.

    Carries an optional machine-readable ``code`` so the fixed-purpose Level 6
    weekly operation can surface a precise, non-secret fail-closed reason —
    e.g. ``ordering_untrusted`` when the feed re-rendered mid-read. The code is
    a short stable token, never a path, host detail, or secret.
    """

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = str(code or "")


# ── Redaction ──────────────────────────────────────────────────────────

def _secret_values() -> list[str]:
    """Literal secret values discovered in the environment, longest first.

    Scanning the environment rather than an allowlist means a newly added
    secret cannot silently escape scrubbing. Longest-first so a value that
    contains another is redacted before its shorter substring.
    """
    values: set[str] = set()
    for key, value in os.environ.items():
        if value and len(value) >= 6 and _SECRET_ENV_RE.search(key):
            values.add(value)
    raw = os.environ.get("KYREX_SCOPED_TOKENS", "")
    if raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            for value in parsed.values():
                if isinstance(value, str) and len(value) >= 6:
                    values.add(value)
    return sorted(values, key=len, reverse=True)


def redact(text) -> str:
    """Scrub secret-shaped material from *text* (never returns None)."""
    if text is None:
        return ""
    scrubbed = str(text)
    for secret in _secret_values():
        scrubbed = scrubbed.replace(secret, "[redacted]")
    for pattern in _HEADER_REDACTIONS:
        scrubbed = pattern.sub(
            lambda m: (m.group(1) + ": [redacted]")
            if ":" in m.group(0)
            else "[redacted]",
            scrubbed,
        )
    return scrubbed


def redact_obj(obj):
    """Recursively redact a JSON-shaped object; sensitive keys drop to a marker."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if _SENSITIVE_KEY_RE.search(str(key)):
                out[key] = "[redacted]"
            else:
                out[key] = redact_obj(value)
        return out
    if isinstance(obj, list):
        return [redact_obj(item) for item in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


# ── Allowlist ──────────────────────────────────────────────────────────

def normalize_host(value) -> str | None:
    """Normalise an allowlist entry or URL host to a bare lowercase hostname.

    Accepts ``example.com``, ``Example.com:443``, ``https://example.com/x`` or
    ``sub.example.com`` and returns ``example.com`` / ``sub.example.com``.
    Returns ``None`` for anything that is not a clean host.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if "://" in raw:
        try:
            raw = urlsplit(raw).hostname or ""
        except ValueError:
            return None
    else:
        raw = raw.split("/")[0].split(":")[0]
    host = raw.strip().strip(".").lower()
    if not host or any(ch in host for ch in " \t/\\"):
        return None
    return host


def parse_allowlist(raw) -> list[str]:
    """Parse ``KYREX_BROWSER_ALLOWLIST`` (JSON list or comma-separated)."""
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        values = parsed if isinstance(parsed, list) else []
    else:
        values = text.split(",")
    out: list[str] = []
    for value in values:
        host = normalize_host(value)
        if host and host not in out:
            out.append(host)
    return out


def host_of(url) -> str | None:
    """Return the bare host of an http(s) URL, or ``None`` if unusable.

    Rejects non-http(s) schemes and any URL carrying userinfo — credentials
    must never travel inside a URL.
    """
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if parts.username or parts.password:
        return None
    return (parts.hostname or "").lower() or None


def _private_or_local_host(host: str) -> bool:
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        name = str(host or "").rstrip(".").lower()
        return (name == "localhost" or name.endswith(
            (".localhost", ".local", ".internal", ".lan", ".home")
        ))


def domain_allowed(url, allowlist) -> tuple[bool, str]:
    """True iff *url*'s host is covered by *allowlist*.

    A host matches an entry exactly or as a subdomain (``a.example.com``
    matches ``example.com``). ``example.com.evil.com`` never matches
    ``example.com``: the boundary is a dot, not a suffix.
    """
    host = host_of(url)
    if host is None:
        return False, "url must be an http(s) url with a host and no credentials"
    entries = [normalize_host(e) for e in (allowlist or [])]
    entries = [e for e in entries if e]
    # Wildcard access is limited to public destinations. Explicit host entries
    # retain their existing matching behavior.
    if "*" in entries:
        if _private_or_local_host(host):
            return False, "private or local network destinations are blocked"
        return True, ""
    for entry in entries:
        if host == entry or host.endswith("." + entry):
            return True, ""
    return False, f"host {host!r} is not in this bot's browser allowlist"


# ── Path confinement ───────────────────────────────────────────────────

def resolve_safe(requested, root) -> tuple[str | None, str | None]:
    """Resolve *requested* under *root*; reject any escape.

    Returns ``(resolved_str, None)`` on success, or ``(None, error)`` when the
    path is empty, or resolves outside the workspace via ``..``, an absolute
    path, or a symlink.
    """
    if requested is None or not str(requested).strip():
        return None, "a workspace-relative path is required"
    root_real = Path(root).resolve(strict=False)
    candidate = Path(str(requested))
    if not candidate.is_absolute():
        candidate = root_real / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        return None, f"path resolution failed: {exc}"
    if str(resolved) != str(root_real) and not str(resolved).startswith(
        str(root_real) + os.sep
    ):
        return None, f"path escapes the bot workspace: {requested!r}"
    return str(resolved), None


def _write_bytes(path: str, data: bytes) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ── Spec parsing ───────────────────────────────────────────────────────

def parse_spec(task_text):
    """Parse a browser task into a validated, normalised action list.

    Accepts ``{"actions": [...]}`` or the shorthand ``{"url": "..."}`` (which
    becomes navigate + read). Raises :class:`SpecError` on anything else —
    importantly, an unrecognised action is refused rather than ignored, so a
    task can never smuggle in a command the operator does not implement.
    """
    try:
        spec = json.loads(task_text)
    except (json.JSONDecodeError, TypeError):
        raise SpecError("browser tasks must be JSON of the form {\"actions\": [...]}")
    if not isinstance(spec, dict):
        raise SpecError("browser task must be a JSON object")
    if LEVEL6_WEEKLY_ACTION in spec:
        return _parse_level6_spec(spec)
    if GOOGLE_MESSAGES_LEVEL6_ACTION in spec:
        return _parse_google_messages_level6_spec(spec)
    if "actions" in spec:
        actions = spec["actions"]
        if not isinstance(actions, list) or not actions:
            raise SpecError("'actions' must be a non-empty list")
    elif spec.get("url"):
        actions = [{"action": "navigate", "url": spec["url"]}, {"action": "read"}]
    else:
        raise SpecError("browser task requires 'actions' or a 'url'")

    normalised = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise SpecError(f"action {index} must be an object")
        name = str(action.get("action") or "").strip().lower()
        if name not in VALID_ACTIONS:
            raise SpecError(f"unsupported action {name!r} at index {index}")
        normalised.append({**action, "action": name})
    return normalised


def _parse_level6_spec(spec: dict) -> list[dict]:
    """Validate the fixed-purpose Level 6 weekly spec.

    The spec carries NO caller-controlled surface: only the fixed marker and
    the pinned page URL (which must equal this module's constant — the
    operation ignores it at run time and always navigates to the constant).
    Any other key, a falsey/truthy-but-not-``True`` flag, or any other URL is
    refused outright.
    """
    flag = spec.get(LEVEL6_WEEKLY_ACTION)
    if flag is not True:
        raise SpecError(f"{LEVEL6_WEEKLY_ACTION!r} must be exactly true")
    url = str(spec.get("url") or "").strip()
    if url != LEVEL6_PAGE_URL:
        raise SpecError(
            f"{LEVEL6_WEEKLY_ACTION!r} may only target the pinned Level 6 "
            "page"
        )
    extra = set(spec) - {LEVEL6_WEEKLY_ACTION, "url"}
    if extra:
        raise SpecError(
            f"{LEVEL6_WEEKLY_ACTION!r} accepts no other keys: "
            + ", ".join(sorted(str(k) for k in extra))
        )
    return [{"action": LEVEL6_WEEKLY_ACTION, "url": LEVEL6_PAGE_URL}]


def _parse_google_messages_level6_spec(spec: dict) -> list[dict]:
    """Validate the fixed-destination Level 6 Google Messages operation."""
    if spec.get(GOOGLE_MESSAGES_LEVEL6_ACTION) is not True:
        raise SpecError(f"{GOOGLE_MESSAGES_LEVEL6_ACTION!r} must be exactly true")
    if str(spec.get("url") or "").strip() != GOOGLE_MESSAGES_BASE_URL:
        raise SpecError("Google Messages sends may only target the pinned web app")
    message = str(spec.get("message") or "")
    lines = message.splitlines()
    weekly_shape = (message.startswith("#L6Workout\n\n🏋️ Level 6 — Workout Week\n")
                    and len(message) <= 4000 and len(lines) == 15
                    and sum(line.startswith("Trainer: ") for line in lines) == 6)
    trainer_change = spec.get("trainer_change")
    if "trainer_change" in spec and trainer_change is None:
        raise SpecError("trainer change identity cannot be null")
    if trainer_change is not None:
        from datetime import date
        if (not isinstance(trainer_change, dict)
                or set(trainer_change) != {"date", "version", "attempt", "owner_key"}
                or type(trainer_change.get("version")) is not int
                or not 1 <= trainer_change["version"] <= 1000000
                or type(trainer_change.get("attempt")) is not int
                or not 0 <= trainer_change["attempt"] <= 10000
                or not isinstance(trainer_change.get("owner_key"), str)
                or not re.fullmatch(r"[a-f0-9]{16}", trainer_change["owner_key"])):
            raise SpecError("invalid trainer change identity")
        try:
            day = date.fromisoformat(trainer_change["date"])
            if day.isoformat() != trainer_change["date"]:
                raise ValueError()
        except (TypeError, ValueError, KeyError):
            raise SpecError("invalid trainer class date") from None
        match = re.fullmatch(r"Your ([A-Za-z]+) class has a new trainer: ([^\r\n]{1,120})\.", message)
        if (not match or match[1] != day.strftime("%A")
                or any(ord(c) < 32 for c in message)
                or "http" in message.lower() or "www." in message.lower()):
            raise SpecError("invalid one-line trainer alert")
    elif not weekly_shape and message != GOOGLE_MESSAGES_DELIVERY_TEST:
        raise SpecError("invalid Level 6 workout message payload")
    extra = set(spec) - {GOOGLE_MESSAGES_LEVEL6_ACTION, "url", "message", "trainer_change"}
    if extra:
        raise SpecError("Google Messages Level 6 spec accepts no other keys")
    return [{"action": GOOGLE_MESSAGES_LEVEL6_ACTION,
             "url": GOOGLE_MESSAGES_BASE_URL, "message": message,
             **({"trainer_change": trainer_change} if trainer_change is not None else {})}]


def is_consequential(*parts) -> bool:
    """True if any word in *parts* is a consequential verb."""
    words = " ".join(str(p or "") for p in parts).lower()
    return any(re.search(rf"\b{re.escape(verb)}\b", words) for verb in CONSEQUENTIAL_VERBS)


def action_operation(action: dict) -> str:
    """Map a structured action to its host operation (dotted form)."""
    name = action["action"]
    if name == GOOGLE_MESSAGES_LEVEL6_ACTION:
        return "messages.send_level6"
    if name == "click":
        if action.get("consequential") or is_consequential(
            action.get("selector"), action.get("label"), action.get("text")
        ):
            return "browser.submit"
        return "browser.click"
    if name == "type":
        return "browser.submit" if action.get("submit") else "browser.type"
    return f"browser.{name}"


def approval_token(operation: str, target) -> str:
    """Deterministic T2 token (exact-match approval) for a consequential op."""
    if OP_TIERS.get(operation, 0) < 2:
        return ""
    verb = operation.split(".")[-1].upper()
    digest = hashlib.sha256(str(target or "").encode("utf-8")).hexdigest()[:4]
    return f"{verb} {digest}"


# ── Protocol ───────────────────────────────────────────────────────────

class Protocol:
    """The stdout/stdin protocol side of the executor."""

    def __init__(self, write=None, read=None):
        self._write = write or self._stdout_write
        self._read = read or (lambda: sys.stdin.readline().strip())

    @staticmethod
    def _stdout_write(line: str) -> None:
        sys.stdout.write(line)
        sys.stdout.flush()

    def redact(self, text) -> str:
        return redact(text)

    def _emit(self, kind: str, value) -> None:
        self._write(f"KYREX_{kind}:{json.dumps(redact_obj(value))}\n")

    def _emit_raw(self, kind: str, value) -> None:
        # For payloads whose values are already individually redacted AND
        # whose keys must survive intact. The approval token is a derived
        # exact-match phrase, not a secret: key-based redaction would rewrite
        # it to "[redacted]" and the operator could never match it.
        self._write(f"KYREX_{kind}:{json.dumps(value)}\n")

    def progress(self, note: dict) -> None:
        self._emit("PROGRESS", note)

    def operation(self, op: str, target, summary: str, detail=None) -> bool:
        """Announce an operation and return True iff the host permits it.

        ALLOW → True (perform now). APPROVE → emit the approval prompt and
        block on stdin until APPROVED/DENIED; True only on APPROVED. Anything
        else → False (the caller stops safely).
        """
        payload = {"op": op, "target": target, "summary": summary}
        if detail:
            payload["detail"] = detail
        self._emit("OPERATION", payload)

        verdict = self._read()
        if verdict == "ALLOW":
            return True
        if verdict == "APPROVE":
            tier = OP_TIERS.get(op, 2)
            request = {"tier": tier, "summary": redact(summary)}
            if detail:
                request["detail"] = redact(detail)
            token = approval_token(op, target)
            if token:
                request["token"] = token
            self._emit_raw("APPROVAL", request)
            return self._read() == "APPROVED"
        return False


class FakeProto:
    """Test double: permits exactly the operations in *allow* (by op name)."""

    def __init__(self, allow=None, deny_after=None):
        self.allow = set(allow) if allow is not None else None  # None = allow all
        self.deny_after = deny_after if deny_after is not None else None
        self.operations: list[str] = []
        self.operation_targets: list[object] = []
        self.progress_notes: list[dict] = []

    def redact(self, text) -> str:
        return redact(text)

    def progress(self, note: dict) -> None:
        self.progress_notes.append(dict(note))

    def operation(self, op: str, target, summary: str, detail=None) -> bool:
        self.operations.append(op)
        self.operation_targets.append(target)
        if self.allow is not None and op not in self.allow:
            return False
        return True


# ── Drivers ────────────────────────────────────────────────────────────

def _strip_html(body: str) -> str:
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", body or "")
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", html.unescape(body)).strip()


def _post_permalink(element) -> str:
    """Best-effort stable permalink for a feed post element (or ``""``).

    Reads only an ``href`` attribute — it never clicks, types, or follows the
    link. A missing permalink is never fatal; it only weakens the metadata.
    """
    for selector in ('a[href*="/posts/"]', 'a[href*="story_fbid="]',
                     'a[href*="/videos/"]'):
        try:
            href = element.locator(selector).first.get_attribute(
                "href", timeout=1000)
        except Exception:  # noqa: BLE001 — a missing link is not a failure
            href = None
        if href:
            return str(href)
    return ""


def _largest_visible_image(element):
    """The primary (largest visible) image inside an article, or ``None``.

    A "THE WEEKLY SIX" post carries its marker as PIXELS inside the post image,
    so the meaningful content is the image, not the caption text. Picking the
    LARGEST visible image skips the tiny avatar/icon chrome and lands on the
    workout graphic. Returns ``None`` when the article has no usable image, so
    the caller can fall back to capturing the whole article element.
    """
    images = element.locator("img")
    best = None
    best_area = 0.0
    try:
        count = images.count()
    except Exception:  # noqa: BLE001 — no readable images
        return None
    for index in range(count):
        image = images.nth(index)
        try:
            if not image.is_visible():
                continue
            box = image.bounding_box()
        except Exception:  # noqa: BLE001 — skip an unmeasurable image
            continue
        if not box:
            continue
        area = float(box.get("width") or 0) * float(box.get("height") or 0)
        if area > best_area:
            best_area = area
            best = image
    return best


def _assert_stable_order(articles, candidates) -> None:
    """Fail closed unless the snapshot still resolves to the SAME order.

    Re-reads each captured descriptor and refuses (``ordering_untrusted``) if
    an element is no longer visible or its permalink changed — which is what a
    feed re-render between the listing pass and the capture pass looks like. A
    shifted/cloned feed must never be captured against stale indices.
    """
    for candidate in candidates:
        try:
            element = articles.nth(int(candidate["index"]))
            visible = element.is_visible()
            permalink = _post_permalink(element)
        except Exception as exc:  # noqa: BLE001 — the DOM moved under us
            raise DriverError(
                "the feed re-rendered while it was being read",
                code="ordering_untrusted",
            ) from exc
        if not visible or permalink != candidate["permalink"]:
            raise DriverError(
                "the feed re-rendered while it was being read",
                code="ordering_untrusted",
            )


def _list_level6_candidates(page, *, max_candidates: int = 6,
                            max_scrolls: int = 8, scroll_step: int = 1200,
                            scan_pause_ms: int = 600) -> list:
    """Deterministic NEWEST-FIRST visible post-article descriptors.

    ``page`` is the duck-typed browser page (Playwright's API shape). This is
    deliberately a CONTENT-BLIND listing: it never reads post text and never
    requires the marker to appear in the caption/DOM — the marker lives in the
    post image, so the caller OCRs each candidate instead. It returns the
    visible ``div[role="article"]`` elements in DOM order (Facebook renders
    newest first), bounded to ``max_candidates`` entries. Scrolling is INTERNAL
    to this function and bounded to ``max_scrolls`` settle-and-retry steps for
    a lazy-loading feed — it is never a generic action and confers no
    click/type/submit capability.

    Ordering is verified before returning (see :func:`_assert_stable_order`):
    a feed that re-rendered mid-read raises ``DriverError(code="ordering_
    untrusted")`` rather than capturing against stale indices.
    """
    articles = page.locator('div[role="article"]')
    cap = max(1, int(max_candidates))
    scroll_budget = max(0, int(max_scrolls))
    for attempt in range(scroll_budget + 1):
        candidates: list = []
        try:
            count = articles.count()
        except Exception as exc:  # noqa: BLE001 — an unreadable feed is fatal
            raise DriverError(
                f"the feed could not be read ({type(exc).__name__})",
                code="ordering_untrusted",
            ) from exc
        for index in range(count):
            if len(candidates) >= cap:
                break
            element = articles.nth(index)
            try:
                if not element.is_visible():
                    continue
            except Exception:  # noqa: BLE001 — skip an unreadable element
                continue
            candidates.append({"index": index,
                               "permalink": _post_permalink(element)})
        if candidates:
            _assert_stable_order(articles, candidates)
            return candidates
        if attempt >= scroll_budget:
            break
        try:
            page.mouse.wheel(0, int(scroll_step))
            page.wait_for_timeout(int(scan_pause_ms))
        except Exception:  # noqa: BLE001 — stop bounded scrolling
            break
    return []


def _level6_photo_key(image) -> str:
    """Stable internal image fingerprint; the URL is never returned.

    Facebook rotates signed CDN query parameters while the Photos grid is
    live.  Those parameters are transport credentials, not image identity, so
    fingerprint only the URL path for HTTP(S) images.  Non-HTTP values retain
    their full value and remain confined to the Browser Host.
    """
    try:
        src = str(image.get_attribute("src") or "")
    except Exception:  # noqa: BLE001
        src = ""
    parsed = urlsplit(src)
    identity = parsed.path if parsed.scheme in {"http", "https"} and parsed.path else src
    return hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:16]


def _safe_level6_photo_viewer_url(value: str) -> str:
    """Return a sanitized same-site Facebook photo URL, or empty string.

    Only the numeric ``fbid`` from the link attached to a visible photo tile
    is retained. Signed/tracking query parameters and arbitrary destinations
    are discarded.
    """
    try:
        parts = urlsplit(str(value or ""))
        query = parse_qs(parts.query)
    except ValueError:
        return ""
    fbid = (query.get("fbid") or [""])[0]
    if (parts.scheme != "https" or parts.hostname != "www.facebook.com"
            or parts.path not in {"/photo/", "/photo.php"}
            or not fbid.isdigit()):
        return ""
    return f"https://www.facebook.com/photo/?fbid={fbid}"


def _same_level6_photo_viewer(requested: str, landed: str) -> bool:
    """Require a photo-viewer navigation to stay on the selected photo."""
    requested_safe = _safe_level6_photo_viewer_url(requested)
    landed_safe = _safe_level6_photo_viewer_url(landed)
    return bool(requested_safe and requested_safe == landed_safe)


def _open_level6_photo_viewer(page, requested: str) -> bool:
    """Allow a slow Facebook load only after the selected photo has committed.

    Facebook sometimes leaves ``goto(wait_until='commit')`` waiting even after
    its address has changed. The image still has to pass the selected grid
    fingerprint check before any pixels are captured.
    """
    try:
        page.goto(requested, wait_until="commit", timeout=25000)
    except Exception as exc:
        if (type(exc).__name__ != "TimeoutError"
                or not _same_level6_photo_viewer(requested, page.url)):
            raise
        _level6_photo_debug("viewer navigation timed out on selected photo")
    return _same_level6_photo_viewer(requested, page.url)


def _find_level6_photo_viewer_image(page, *, expected_key: str = "",
                                    attempts: int = 10, wait_ms: int = 500):
    """Wait for the selected photo, matching its grid image fingerprint.

    Facebook can keep the requested ``fbid`` in the URL while displaying an
    unrelated image (for example, the page cover). The URL alone therefore
    does not establish which pixels are being captured.
    """
    attempt_cap = max(1, min(int(attempts), 10))
    image_count = 0
    largest_box = (0, 0)
    selected_matches = 0
    for attempt in range(attempt_cap):
        best, best_area = None, 0
        try:
            images = page.locator("img")
            count = min(images.count(), 200)
        except Exception:  # noqa: BLE001 — allow one of the bounded retries
            count = 0
        for index in range(count):
            image = images.nth(index)
            try:
                if not image.is_visible():
                    continue
                box = image.bounding_box()
                if not box:
                    continue
                width = float(box.get("width") or 0)
                height = float(box.get("height") or 0)
            except Exception:  # noqa: BLE001 — a transient DOM element
                continue
            image_count = max(image_count, count)
            if width * height > largest_box[0] * largest_box[1]:
                largest_box = (int(width), int(height))
            if expected_key:
                try:
                    matches_selected = _level6_photo_key(image) == expected_key
                except Exception:  # noqa: BLE001 — tolerate a changing image
                    matches_selected = False
                if not matches_selected:
                    continue
                selected_matches += 1
            if width < 300 or height < 300 or not 0.75 <= width / height <= 1.35:
                continue
            area = width * height
            if area > best_area:
                best, best_area = image, area
        if best is not None:
            return best
        if attempt + 1 < attempt_cap:
            page.wait_for_timeout(max(0, min(int(wait_ms), 1000)))
    if expected_key:
        _level6_photo_debug(
            "viewer selected-image check failed "
            f"(image_elements={image_count}, "
            f"largest_box={largest_box[0]}x{largest_box[1]}, "
            f"selected_matches={selected_matches})"
        )
    return None


def _list_level6_photos(page, *, max_candidates: int = 6) -> list:
    """List visible Facebook photo links in DOM (newest-first) order.

    Facebook's Photos page includes other large images, including Messenger
    previews. Pin each image element before reading its bounds and link, then
    accept only a numeric Facebook photo ID. A later screenshot uses that
    selected ID; DOM indices alone are not stable across page updates.
    """
    images = page.locator("img")
    candidates = []
    try:
        count = images.count()
    except Exception as exc:  # noqa: BLE001
        raise DriverError("the Photos page could not be read", code="ordering_untrusted") from exc
    for index in range(count):
        if len(candidates) >= max(1, int(max_candidates)):
            break
        try:
            # A Locator re-resolves its index on every call. Facebook may
            # insert Messenger images between the visibility and link reads.
            image = images.nth(index).element_handle(timeout=1000)
            if image is None:
                continue
            if not image.is_visible():
                continue
            box = image.bounding_box()
            href = image.evaluate("el => el.closest('a[href]')?.href || ''")
        except Exception:  # noqa: BLE001
            continue
        if not box or float(box.get("width") or 0) < 160 or float(box.get("height") or 0) < 160:
            continue
        viewer_url = _safe_level6_photo_viewer_url(str(href or ""))
        if not viewer_url:
            continue
        candidates.append({
            "index": index,
            "key": _level6_photo_key(image),
            # Bind a safe photo identity while this DOM snapshot is being
            # enumerated. The grid can virtualize/reorder before capture.
            "viewer_url": viewer_url,
        })
    _level6_photo_debug(f"verified photo candidates: {len(candidates)}")
    return candidates


def _level6_grid_photo_for_capture(page, index: int, viewer_url: str):
    """Pin the selected visible photo even if Facebook reordered the grid."""
    images = page.locator("img")
    if not viewer_url:
        image = images.nth(index)
        if not image.is_visible() or not image.bounding_box():
            raise DriverError("the Photos grid slot is unavailable",
                              code="ordering_untrusted")
        return image

    try:
        count = min(images.count(), 200)
    except Exception as exc:  # noqa: BLE001
        raise DriverError("the Photos grid could not be read",
                          code="ordering_untrusted") from exc
    matches = []
    for position in range(count):
        image = images.nth(position)
        try:
            if not image.is_visible():
                continue
            box = image.bounding_box()
            if not box or min(float(box.get("width") or 0),
                              float(box.get("height") or 0)) < 160:
                continue
            href = image.evaluate("el => el.closest('a[href]')?.href || ''")
            if _safe_level6_photo_viewer_url(str(href or "")) != viewer_url:
                continue
            # A locator's nth(index) may resolve to a different image on its
            # next call. Pin this DOM node and verify its ID once more before
            # modifying its style and taking the screenshot.
            pinned = image.element_handle(timeout=1000)
            if pinned is None:
                continue
            pinned_href = pinned.evaluate(
                "el => el.closest('a[href]')?.href || ''")
            if _safe_level6_photo_viewer_url(str(pinned_href or "")) != viewer_url:
                continue
        except Exception:  # noqa: BLE001 — the tile may have detached
            continue
        matches.append(pinned)
        if len(matches) > 1:
            _level6_photo_debug("grid photo ID ambiguous")
            raise DriverError("the selected Photos tile is ambiguous",
                              code="ordering_untrusted")
    if not matches:
        _level6_photo_debug("grid photo ID unavailable")
        raise DriverError("the selected Photos tile is unavailable",
                          code="ordering_untrusted")
    _level6_photo_debug("grid photo ID rebound")
    return matches[0]


def _level6_photo_debug(message: str) -> None:
    """Opt-in host-local diagnostics without photo IDs or image URLs."""
    if os.environ.get("KYREX_LEVEL6_OCR_DEBUG") == "1":
        print(f"[level6-photo] {message}", file=sys.stderr, flush=True)


class LocalDriver:
    """Deterministic, dependency-free transport (``KYREX_BROWSER_DRIVER=local``).

    Fetches http(s) from the requested URL only (the executor's allowlist is
    the control) and never exposes response headers, cookies or auth state.
    Used by the smoke test and by environments without Chromium; it is not a
    general browser and executes no JavaScript.
    """

    def __init__(self, session_dir: Path):
        self.session_dir = Path(session_dir)
        self._url = ""
        self._title = ""
        self._body = ""

    def open(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def navigate(self, url: str) -> None:
        request = urllib.request.Request(
            url, headers={"User-Agent": "KyrexBrowserOperator/1.0"}
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            self._body = response.read(1_000_000).decode("utf-8", "replace")
            self._url = response.geturl()
        match = re.search(r"(?is)<title>(.*?)</title>", self._body)
        self._title = _strip_html(match.group(1)) if match else ""

    def current_url(self) -> str:
        return self._url

    def title(self) -> str:
        return self._title

    def text(self) -> str:
        return _strip_html(self._body)

    def click(self, selector: str) -> None:  # noqa: ARG002 — deterministic no-op
        return None

    def type(self, selector: str, text: str) -> None:  # noqa: ARG002
        return None

    def upload(self, selector: str, path: str) -> None:  # noqa: ARG002
        return None

    def download(self, url: str) -> bytes:
        request = urllib.request.Request(
            url, headers={"User-Agent": "KyrexBrowserOperator/1.0"}
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read(1_000_000)

    def screenshot(self, path: str) -> None:
        # Deliberately does NOT capture page text — a screenshot must never
        # become a side channel for secrets rendered on the page.
        _write_bytes(path, b"\x89PNG\r\n\x1a\nkyrex-browser-operator\n")

    def scan_level6_candidates(self, **kwargs):
        # The local driver is a dependency-free HTTP fetch: it has no DOM, so
        # it cannot enumerate post articles. Fail closed.
        raise DriverError("the local driver cannot locate Facebook posts",
                          code="locate_failed")

    def capture_level6_candidate(self, descriptor, path: str) -> None:
        raise DriverError("the local driver cannot capture a post element",
                          code="capture_failed")

    def scan_level6_photos(self, **kwargs):
        raise DriverError("the local driver cannot locate Facebook photo elements",
                          code="locate_failed")

    def capture_level6_photo(self, descriptor, path: str) -> None:
        raise DriverError("the local driver cannot capture a photo element",
                          code="capture_failed")

    def level6_photo_viewer_target(self, descriptor):  # noqa: ARG002
        return ""

    def close(self) -> None:
        return None


class PlaywrightDriver:
    """Real Chromium transport (default). Lazy-imports Playwright."""

    def __init__(self, session_dir: Path, endpoint: str = "",
                 managed: bool | None = None):
        self.session_dir = Path(session_dir)
        self.endpoint = (endpoint or "").strip()
        self.managed = _is_managed() if managed is None else bool(managed)
        # A managed CDP guest must never tear down the host's context/browser:
        # the persistent profile has to outlive this process. Everything else
        # (a browser we launched ourselves, managed or not) is ours to close.
        self._owns = not (self.managed and self.endpoint)
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def open(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - exercised only with real deps
            raise DriverError(
                "playwright is not available; install it or set "
                f"KYREX_BROWSER_DRIVER=local ({exc})"
            )
        self.session_dir.mkdir(parents=True, exist_ok=True)
        executable = os.environ.get("KYREX_BROWSER_EXECUTABLE", "/usr/bin/chromium")
        headless = str(os.environ.get("KYREX_BROWSER_HEADLESS", "1")).strip().lower() \
            not in {"0", "false", "no", "off"}
        self._pw = sync_playwright().start()
        if self.endpoint:
            self._browser = self._pw.chromium.connect_over_cdp(self.endpoint)
            if self.managed:
                # Reuse the host's persistent default context. A fresh
                # new_context() here would be a throwaway incognito profile, so
                # any login the operator performed would vanish on every run.
                contexts = list(self._browser.contexts)
                self._context = (
                    contexts[0] if contexts else self._browser.new_context()
                )
                pages = list(self._context.pages)
                self._page = pages[0] if pages else self._context.new_page()
            else:
                self._context = self._browser.new_context()
                self._page = self._context.new_page()
        elif self.managed:
            # The persistent profile on disk IS the managed session:
            # launch_persistent_context reopens this user-data-dir every run,
            # so the account stays logged in across runs.
            self._browser = None
            self._context = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.session_dir),
                headless=headless,
                executable_path=executable,
                args=["--no-sandbox"],
            )
            pages = list(self._context.pages)
            self._page = pages[0] if pages else self._context.new_page()
        else:
            self._browser = self._pw.chromium.launch(
                headless=headless,
                executable_path=executable,
                args=["--no-sandbox"],
            )
            self._context = self._browser.new_context(
                user_data_dir=str(self.session_dir)
            )
            self._page = self._context.new_page()

    def navigate(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded", timeout=30000)

    def current_url(self) -> str:
        return self._page.url

    def title(self) -> str:
        return self._page.title()

    def text(self) -> str:
        return self._page.locator("body").inner_text(timeout=10000)

    def links(self) -> list:
        # Read only the loaded document: no clicks, fetches or navigation.
        return self._page.locator("a[href]").evaluate_all("""nodes => nodes
            .filter(a => a.getClientRects().length)
            .slice(0, 500).map(a => ({url: a.href,
                label: (a.innerText || a.getAttribute('aria-label') ||
                    a.title || '').slice(0, 120)}))""")

    def facebook_evidence(self, allowlist) -> str:
        from facebook_read import read_feed
        return read_feed(self._page, allowed=lambda url: domain_allowed(
            url, allowlist)[0])

    def click(self, selector: str) -> None:
        self._page.locator(selector).click(timeout=15000)

    def type(self, selector: str, text: str) -> None:
        self._page.locator(selector).fill(text, timeout=15000)

    def upload(self, selector: str, path: str) -> None:
        self._page.set_input_files(selector, path, timeout=15000)

    def download(self, url: str) -> bytes:
        response = self._context.request.get(url)
        return response.body()

    def screenshot(self, path: str) -> None:
        self._page.screenshot(path=path, full_page=True)

    def scan_level6_candidates(self, *, max_candidates: int = 6,
                               max_scrolls: int = 8, scroll_step: int = 1200,
                               scan_pause_ms: int = 600) -> list:
        """Deterministic NEWEST-FIRST visible post-article descriptors.

        Content-blind: it never reads post TEXT and never requires the marker
        in the caption/DOM (the marker is PIXELS inside the post image, so the
        caller OCRs each candidate). Bounded and side-effect-free beyond
        bounded scrolling — see :func:`_list_level6_candidates`.
        """
        return _list_level6_candidates(
            self._page, max_candidates=max_candidates, max_scrolls=max_scrolls,
            scroll_step=scroll_step, scan_pause_ms=scan_pause_ms,
        )

    def capture_level6_candidate(self, descriptor, path: str) -> None:
        """Capture ONE candidate to a host-local PNG.

        Prefers the primary (largest visible) POST IMAGE — where the marker
        pixels actually are — and falls back to the whole article element when
        the article carries no usable image. Never reads or returns text.
        """
        index = int((descriptor or {}).get("index"))
        article = self._page.locator('div[role="article"]').nth(index)
        image = _largest_visible_image(article)
        if image is not None:
            image.screenshot(path=path, timeout=15000)
            return
        article.screenshot(path=path, timeout=15000)

    def scan_level6_photos(self, *, max_candidates: int = 6) -> list:
        # Facebook returns DOMContentLoaded before the Photos grid finishes
        # hydrating. The live page had three photo links immediately and 17
        # three seconds later; selecting the first snapshot can miss the
        # newest week or report no candidate. Wait briefly, then require two
        # consecutive snapshots of the selected photo IDs to agree.
        self._page.wait_for_timeout(3000)
        previous_ids = None
        saw_photos = False
        for attempt in range(4):
            if attempt:
                self._page.wait_for_timeout(1500)
            photos = _list_level6_photos(self._page, max_candidates=max_candidates)
            ids = tuple(photo["viewer_url"] for photo in photos)
            saw_photos |= bool(ids)
            if ids and ids == previous_ids:
                _level6_photo_debug(f"stable photo candidates: {len(ids)}")
                return photos
            previous_ids = ids
        if saw_photos:
            raise DriverError("the Photos grid did not settle",
                              code="ordering_untrusted")
        return []

    def level6_photo_viewer_target(self, descriptor) -> str:
        """Get a sanitized, same-site photo-viewer URL from a grid image link."""
        image = self._page.locator("img").nth(int((descriptor or {}).get("index")))
        href = image.evaluate(
            "el => el.closest('a[href]')?.href || ''")
        return _safe_level6_photo_viewer_url(href)

    def _capture_photo_viewer(self, url: str, path: str,
                              expected_key: str = ""):
        """Open the constrained Facebook photo viewer in a temporary tab."""
        safe_url = _safe_level6_photo_viewer_url(url)
        if not safe_url:
            return None
        page = self._context.new_page()
        stage = "navigation"
        try:
            # Facebook's JS-heavy page can keep DOMContentLoaded pending even
            # though the selected photo document has already committed. Wait
            # for the commit, then verify the selected photo's identity.
            if not _open_level6_photo_viewer(page, safe_url):
                return None
            stage = "image lookup"
            best = _find_level6_photo_viewer_image(
                page, expected_key=expected_key)
            if best is None:
                return None
            stage = "image preparation"
            prior = best.evaluate("""async el => {
                const prior = {cssText: el.style.cssText,
                               sizes: el.getAttribute('sizes')};
                if (el.getAttribute('srcset')) {
                    el.setAttribute('sizes', '1600px');
                    await new Promise(resolve => requestAnimationFrame(
                        () => requestAnimationFrame(resolve)));
                    await Promise.race([el.decode().catch(() => {}),
                        new Promise(resolve => setTimeout(resolve, 5000))]);
                }
                const nw=Math.max(1,Number(el.naturalWidth)||1);
                const nh=Math.max(1,Number(el.naturalHeight)||1);
                const scale=Math.min(1,1600/nw,1600/nh);
                el.style.setProperty('width', `${Math.round(nw*scale)}px`, 'important');
                el.style.setProperty('height', `${Math.round(nh*scale)}px`, 'important');
                el.style.setProperty('object-fit','contain','important');
                return { ...prior, width:nw, height:nh };
            }""")
            stage = "screenshot"
            best.screenshot(path=path, timeout=15000)
            return {"width": int(prior.get("width", 0)),
                    "height": int(prior.get("height", 0)),
                    "capture_mode": "photo_viewer"}
        except Exception as exc:
            _level6_photo_debug(
                f"viewer {stage} failed: {type(exc).__name__}")
            raise
        finally:
            page.close()

    def capture_level6_photo(self, descriptor, path: str, viewer_url="") -> None:
        # The photo viewer URL was read from the selected visible grid tile.
        # Prefer that stable, sanitized photo ID over reusing the tile's DOM
        # index: Facebook virtualizes/reorders its Photos grid while images
        # load, which can make a still-correct selected tile appear missing at
        # capture time. The final URL is checked against the requested photo
        # ID before any pixels are accepted.
        safe_viewer_url = _safe_level6_photo_viewer_url(viewer_url)
        if safe_viewer_url:
            expected_key = str((descriptor or {}).get("key") or "")
            if expected_key:
                try:
                    viewer_info = self._capture_photo_viewer(
                        safe_viewer_url, path, expected_key=expected_key)
                except Exception as exc:  # noqa: BLE001 — try the same verified grid photo
                    _level6_photo_debug(f"viewer failed: {type(exc).__name__}")
                    viewer_info = None
            else:
                _level6_photo_debug("viewer skipped: selected image fingerprint missing")
                viewer_info = None
            if viewer_info:
                _level6_photo_debug("viewer captured")
                return viewer_info
            _level6_photo_debug("viewer unavailable; verifying grid photo ID")

        # Facebook can reorder its grid before this fallback. When the
        # selected tile has a photo ID, resolve and pin that same ID rather
        # than trusting its old DOM position. Without an ID, keep the bounded
        # slot and let strict image OCR + the exact Glofox date join validate.
        index = int((descriptor or {}).get("index"))
        image = _level6_grid_photo_for_capture(
            self._page, index, safe_viewer_url)
        # Photos-grid thumbnails commonly choose a tiny srcset candidate for
        # the grid cell. Ask the browser for the largest responsive candidate
        # first, then render the resulting source at its natural aspect ratio
        # (bounded to 1600x1600). This stays on the already loaded image and
        # performs no click, navigation, or arbitrary URL fetch.
        prior_style = image.evaluate(
            """async el => {
                const prior = {
                    cssText: el.style.cssText,
                    sizes: el.getAttribute('sizes'),
                    srcsetCount: (el.getAttribute('srcset') || '')
                        .split(',').filter(Boolean).length,
                };
                if (el.getAttribute('srcset')) {
                    el.setAttribute('sizes', '1600px');
                    await new Promise(resolve => requestAnimationFrame(
                        () => requestAnimationFrame(resolve)));
                    await Promise.race([
                        el.decode().catch(() => {}),
                        new Promise(resolve => setTimeout(resolve, 5000)),
                    ]);
                }
                const nw = Math.max(1, Number(el.naturalWidth) || 1);
                const nh = Math.max(1, Number(el.naturalHeight) || 1);
                prior.naturalWidth = nw;
                prior.naturalHeight = nh;
                const scale = Math.min(1, 1600 / nw, 1600 / nh);
                el.style.setProperty('width', `${Math.max(1, Math.round(nw * scale))}px`, 'important');
                el.style.setProperty('height', `${Math.max(1, Math.round(nh * scale))}px`, 'important');
                el.style.setProperty('min-width', '0', 'important');
                el.style.setProperty('min-height', '0', 'important');
                el.style.setProperty('max-width', 'none', 'important');
                el.style.setProperty('max-height', 'none', 'important');
                el.style.setProperty('object-fit', 'contain', 'important');
                el.style.setProperty('object-position', 'center', 'important');
                return prior;
            }"""
        )
        try:
            image.screenshot(path=path, timeout=15000)
        finally:
            image.evaluate(
                """(el, prior) => {
                    el.style.cssText = String(prior?.cssText || '');
                    if (prior?.sizes == null) el.removeAttribute('sizes');
                    else el.setAttribute('sizes', String(prior.sizes));
                }""",
                prior_style,
            )
        info = {
            "width": int(prior_style.get("naturalWidth", 0)),
            "height": int(prior_style.get("naturalHeight", 0)),
            "srcset_candidates": int(prior_style.get("srcsetCount", 0)),
            "capture_mode": "grid_tile",
        }
        return info

    def close(self) -> None:
        # A managed CDP guest only detaches: closing the host's context/browser
        # would destroy the persistent profile the next run must reconnect to.
        if self._owns:
            for closer in (self._context, self._browser):
                try:
                    if closer is not None:
                        closer.close()
                except Exception:
                    pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass


def _is_managed() -> bool:
    """Whether this run manages a persistent profile (a host-managed session).

    An explicit ``KYREX_BROWSER_MANAGED`` wins; when it is unset, the presence
    of a host-supplied ``KYREX_BROWSER_SESSION_DIR`` marks the run as managed,
    since that directory only ever exists for a managed persistent session.
    Managed mode is what makes the profile persist — see ``PlaywrightDriver``.
    """
    raw = (os.environ.get("KYREX_BROWSER_MANAGED", "") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return bool((os.environ.get("KYREX_BROWSER_SESSION_DIR", "") or "").strip())


def managed_endpoint(bot_id: str) -> str:
    """Per-Bot managed-browser CDP endpoint, if configured."""
    raw = os.environ.get("KYREX_BROWSER_WS_ENDPOINTS_JSON", "")
    if not raw.strip().startswith("{"):
        return ""
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if isinstance(mapping, dict):
        return str(mapping.get(bot_id) or "").strip()
    return ""


def _slug(value, default: str = "unbound") -> str:
    """Sanitize an id into a single safe path component (no separators, no ..)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    cleaned = re.sub(r"\.{2,}", "_", cleaned)   # collapse any ".." run
    cleaned = cleaned.strip("._")
    return cleaned or default


def browser_session_dir(root, bot_id: str, owner: str) -> Path:
    """Isolated session directory: distinct per Bot AND per owner."""
    return (
        Path(root)
        / "browser-sessions"
        / f"bot-{_slug(bot_id)}"
        / f"owner-{_slug(owner)}"
    )


def build_driver(kind: str, session_dir, bot_id: str, managed=None):
    if kind == "local":
        return LocalDriver(session_dir)
    return PlaywrightDriver(
        session_dir, endpoint=managed_endpoint(bot_id), managed=managed
    )


# ── Action execution ───────────────────────────────────────────────────

def _result_error(message: str, final_response: str = "", artifacts=None) -> dict:
    return {
        "status": "error",
        "final_response": final_response or "",
        "browser_artifacts": list(artifacts or []),
        "errors": [message],
    }


def _join_text(proto, texts) -> str:
    return proto.redact("\n\n".join(t for t in texts if t))[:MAX_TEXT]


def _safe_text(proto, text) -> str:
    value = proto.redact(text) if text else ""
    return value[:MAX_TEXT]


def _discovered_links(proto, links) -> str:
    """Bounded document evidence; a discovered link is not a visited source."""
    if not isinstance(links, list):
        return ""
    valid = []
    seen = set()
    for link in links[:500]:
        if not isinstance(link, dict) or not isinstance(link.get("url"), str):
            continue
        url = link["url"]
        try:
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or len(url) > 1000
                    or any(_SECRET_ENV_RE.search(key) for key in parse_qs(parsed.query))):
                continue
        except ValueError:
            continue
        if url in seen:
            continue
        seen.add(url)
        label = " ".join(str(link.get("label") or "Link").split())[:120]
        social = parsed.hostname.lower() in {"facebook.com", "www.facebook.com", "m.facebook.com"}
        valid.append((not social, _safe_text(proto, label + " — " + url)))
    valid.sort(key=lambda entry: entry[0])
    lines = []
    length = 0
    for _, line in valid[:20]:
        if length + len(line) > 1800:
            break
        lines.append(line)
        length += len(line)
    return "Discovered page links (not visited):\n" + "\n".join(lines) if lines else ""


def _rel(path: str, root: Path) -> str:
    try:
        return str(Path(path).relative_to(Path(root)))
    except ValueError:
        return str(path)


def _summarize(name: str, op: str, target: str, action: dict) -> str:
    if name == "navigate":
        return f"navigate to {target}"
    if name == "read":
        return "read page"
    if name == "screenshot":
        return f"screenshot {target}" if target else "screenshot"
    if name == "type":
        verb = "type and submit in" if action.get("submit") else "type into"
        return f"{verb} {target}"
    if name == "click":
        return f"click {target}"
    return f"{name} {target}".strip()


def _detail(action: dict, op: str) -> str:
    if op.endswith(".submit"):
        return "consequential action requires explicit approval"
    if op.endswith(".delete"):
        return "delete control requires explicit approval"
    return ""


def _run_google_messages_level6(driver, proto, action, *, root, allowlist):
    """Send one validated weekly post to the host-local fixed conversation."""
    send_started = False
    trainer = action.get("trainer_change")
    def fail(reason):
        result = _result_error(reason)
        if trainer:
            result["delivery_state"] = "unknown" if send_started else "failed"
        return result
    if trainer:
        from datetime import date, datetime, time
        from zoneinfo import ZoneInfo
        start = datetime.combine(date.fromisoformat(trainer["date"]), time(8, 30), ZoneInfo("America/New_York"))
        if start.timestamp() <= datetime.now().timestamp():
            return fail("trainer class has already started")
    conversation_url = str(
        os.environ.get("KYREX_GOOGLE_MESSAGES_CONVERSATION_URL") or ""
    ).strip()
    parts = urlsplit(conversation_url)
    if (parts.scheme != "https" or parts.hostname != "messages.google.com"
            or not parts.path.startswith("/web/conversations/")
            or parts.query or parts.fragment or parts.username or parts.password):
        return fail("fixed Google Messages conversation is not configured")
    allowed, reason = domain_allowed(conversation_url, allowlist)
    if not allowed:
        return fail(f"Google Messages destination blocked: {reason}")

    message = str(action.get("message") or "")
    receipt_key = (json.dumps([conversation_url, trainer], sort_keys=True) if trainer else message)
    digest = hashlib.sha256(receipt_key.encode("utf-8")).hexdigest()
    receipt_path = Path(driver.session_dir) / ".kyrex-level6-message-receipts.json"
    try:
        receipts = json.loads(receipt_path.read_text("utf-8")) \
            if receipt_path.exists() else []
    except Exception:
        return fail("Google Messages send receipt store is unreadable")
    if not isinstance(receipts, list):
        return fail("Google Messages send receipt store is malformed")
    if digest in receipts:
        return {"status": "no_changes",
                "final_response": ("Kyrex delivery test was already sent to the group."
                                   if message == GOOGLE_MESSAGES_DELIVERY_TEST else
                                   "Trainer alert was already sent to the group." if trainer else "#L6Workout was already sent to the group."),
                "browser_artifacts": [], "errors": []}

    if not proto.operation(
        "messages.send_level6", "fixed-group",
        ("send the confirmed trainer-change alert to the fixed group" if trainer else
         "send the validated #L6Workout weekly post to the fixed group"), ""
    ):
        return fail("messages.send_level6 denied")

    try:
        driver.navigate(conversation_url)
        if str(driver.current_url() or "").rstrip("/") != conversation_url.rstrip("/"):
            return fail("Google Messages did not open the fixed conversation")
        page = driver._page
        composer = None
        for selector in (
            '[contenteditable="true"][role="textbox"]',
            'textarea[aria-label*="message" i]',
            'textarea',
        ):
            matches = [page.locator(selector).nth(i)
                       for i in range(page.locator(selector).count())
                       if page.locator(selector).nth(i).is_visible()]
            if len(matches) == 1:
                composer = matches[0]
                break
            if len(matches) > 1:
                return fail("Google Messages composer is ambiguous")
        if composer is None:
            return fail("Google Messages composer is unavailable; pair the profile")
        if trainer:
            draft = composer.input_value() if composer.evaluate("el => 'value' in el") else composer.inner_text()
            if str(draft or "").strip():
                return fail("Google Messages has an unsent draft; it was left untouched")
        composer.fill(message, timeout=15000)

        send_button = None
        for selector in (
            'button[aria-label*="send" i]',
            '[role="button"][aria-label*="send" i]',
        ):
            matches = [page.locator(selector).nth(i)
                       for i in range(page.locator(selector).count())
                       if page.locator(selector).nth(i).is_visible()]
            if len(matches) == 1:
                send_button = matches[0]
                break
            if len(matches) > 1:
                return fail("Google Messages send control is ambiguous")
        if send_button is None:
            return fail("Google Messages send control is unavailable")
        if trainer and start.timestamp() <= datetime.now().timestamp():
            return fail("trainer class has already started")
        if str(driver.current_url() or "").rstrip("/") != conversation_url.rstrip("/"):
            return fail("Google Messages left the fixed conversation")
        send_started = True  # A click timeout may happen AFTER the message was accepted.
        send_button.click(timeout=15000)
        page.wait_for_timeout(1200)
        remaining = composer.input_value() if composer.evaluate(
            "el => 'value' in el") else composer.inner_text()
        if str(remaining or "").strip():
            return fail("Google Messages did not confirm the send")

        updated = (receipts + [digest])[-1024:]
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        temp = receipt_path.with_suffix(".tmp")
        temp.write_text(json.dumps(updated), encoding="utf-8")
        os.replace(temp, receipt_path)
        proto.progress({"action": GOOGLE_MESSAGES_LEVEL6_ACTION,
                        "op": "messages.send_level6"})
        return {"status": "ok",
                "final_response": ("✅ Sent Kyrex delivery test to the group."
                                   if message == GOOGLE_MESSAGES_DELIVERY_TEST else
                                   "Sent trainer alert to the group." if trainer else "✅ Sent #L6Workout to the group."),
                "browser_artifacts": [], "errors": []}
    except Exception as exc:
        return fail(
            f"Google Messages send failed: {type(exc).__name__}: "
            f"{proto.redact(str(exc))}"
        )


def run_actions(actions, driver, *, root, allowlist, proto=None,
                session_ref: str = "") -> dict:
    """Execute a validated action list, returning the executor result dict.

    Enforces the allowlist on every URL and the workspace confinement on every
    path *before* requesting approval, then performs each action only after the
    host (and, when required, the operator) permits it. Any denial stops the
    run immediately with a result — never a hang.

    *session_ref* is the host-managed browser session id (NON-secret). When
    present it is echoed on progress and in the result so a UI can correlate a
    task with the persistent session it ran against. It is an identifier, never
    a credential — the session's token stays in the host's sealed blob and is
    never sent to this executor.
    """
    proto = proto if proto is not None else Protocol()
    root = Path(root)
    entries = [e for e in (normalize_host(a) for a in (allowlist or [])) if e]
    if not entries:
        return _result_error(
            "no browser allowlist configured for this bot — all navigation is blocked"
        )

    texts: list[str] = []
    artifacts: list[str] = []
    sources: list[str] = []
    did_write = False

    try:
        driver.open()
    except DriverError as exc:
        return _result_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _result_error(
            f"browser operator failed to start: {type(exc).__name__}: {proto.redact(str(exc))}"
        )

    # Correlate this run with the host's persistent session. The ref is an
    # identifier, not a secret; progress goes through redact_obj regardless.
    if session_ref:
        proto.progress({"browser": "managed", "ref": session_ref, "state": "connected"})

    try:
        if len(actions) == 1 and actions[0]["action"] == LEVEL6_WEEKLY_ACTION:
            # The fixed-purpose Level 6 weekly operation. Lazy import: the
            # module ships in the Browser Host image next to this operator; a
            # process without it (the Cloud never runs this action) fails
            # CLOSED rather than pretending the operation succeeded.
            try:
                import level6_post as _level6
            except Exception as exc:  # noqa: BLE001 — fail closed
                return _result_error(
                    f"{LEVEL6_WEEKLY_ACTION} operation unavailable: "
                    f"{type(exc).__name__}"
                )
            return _level6.run_level6_weekly(
                driver, proto, root=root, allowlist=entries
            )
        if len(actions) == 1 and actions[0]["action"] == GOOGLE_MESSAGES_LEVEL6_ACTION:
            return _run_google_messages_level6(
                driver, proto, actions[0], root=root, allowlist=entries
            )

        for index, action in enumerate(actions):
            name = action["action"]
            url = str(action.get("url") or "").strip()
            selector = str(action.get("selector") or "").strip()
            path_arg = str(action.get("path") or "").strip()

            # 1. Allowlist gate — before any approval or execution.
            if name in ("navigate", "download"):
                if not url:
                    return _result_error(f"{name} requires a url")
                allowed, reason = domain_allowed(url, entries)
                if not allowed:
                    return _result_error(f"blocked {name}: {reason}")

            # 2. Workspace confinement — before any approval or execution.
            resolved_path = None
            if name in ("screenshot", "upload", "download"):
                if name in ("upload", "download") and not path_arg:
                    return _result_error(f"{name} requires a workspace-relative path")
                if name == "screenshot" and not path_arg:
                    path_arg = f"browser-artifacts/screenshot-{index:02d}.png"
                resolved_path, err = resolve_safe(path_arg, root)
                if err:
                    return _result_error(err)

            # 3. Selector gate.
            if name in ("click", "type", "upload", "delete") and not selector:
                return _result_error(f"{name} requires a selector")

            # 4. Live-origin re-check — a page must never carry an action off
            #    the allowlist (redirect, meta-refresh, JS navigation).
            live = ""
            if name != "navigate":
                try:
                    live = str(driver.current_url() or "")
                except Exception:
                    live = ""
                if live:
                    allowed, reason = domain_allowed(live, entries)
                    if not allowed:
                        return _result_error(
                            f"page left the allowlist before {name}: {reason}"
                        )

            op = action_operation(action)
            target = url or selector or path_arg
            summary = _summarize(name, op, target, action)
            detail = _detail(action, op)

            # 5. Approval gate.
            if not proto.operation(op, target, summary, detail):
                return _result_error(
                    f"{op} denied",
                    final_response=_join_text(proto, texts),
                    artifacts=artifacts,
                )

            # 6. Perform.
            if name == "navigate":
                driver.navigate(url)
                final_url = str(driver.current_url() or url)
                allowed, reason = domain_allowed(final_url, entries)
                if not allowed:
                    return _result_error(f"navigation left the allowlist: {reason}")
                title = _safe_text(proto, driver.title())
                if title:
                    texts.insert(0, title)
            elif name == "read":
                actual = str(driver.current_url() or "")
                allowed, reason = domain_allowed(actual, entries)
                if not allowed:
                    return _result_error(f"page left the allowlist during read: {reason}")
                evidence = ""
                # Facebook's DOM alt text is not the graphic's contents.
                # The host reads rendered post images locally as part of read;
                # no pixel artifacts or new generic interaction are exposed.
                if (urlsplit(actual).hostname in {
                        "facebook.com", "www.facebook.com", "web.facebook.com", "m.facebook.com"}
                        and callable(getattr(driver, "facebook_evidence", None))):
                    try:
                        evidence = _safe_text(proto, driver.facebook_evidence(entries))
                    except ImportError:
                        evidence = "Facebook image/feed reader unavailable on this host; image text unverified."
                text = _safe_text(proto, driver.text())
                links = ""
                if callable(getattr(driver, "links", None)):
                    try:
                        links = _discovered_links(proto, driver.links())
                    except Exception:
                        pass  # A link-extraction failure must not discard page text.
                actual = str(driver.current_url() or "")
                allowed, reason = domain_allowed(actual, entries)
                if not allowed:
                    return _result_error(f"page left the allowlist during read: {reason}")
                # Keep observed links at the front of this bounded read so a
                # footer social icon survives truncation of a long document.
                texts.append("\n\n".join(filter(None, [evidence, links, text])))
                actual_url = _safe_text(proto, actual)
                if actual_url and actual_url not in sources:
                    sources.append(actual_url)
            elif name in ("click", "delete"):
                driver.click(selector)
                # A click the operator approved as browser.submit is a
                # consequential write; a plain click is not.
                if name == "delete" or op.endswith(".submit"):
                    did_write = True
            elif name == "type":
                driver.type(selector, str(action.get("text") or ""))
                if action.get("submit"):
                    did_write = True
            elif name == "upload":
                driver.upload(selector, resolved_path)
                did_write = True
            elif name == "download":
                _write_bytes(resolved_path, driver.download(url))
                artifacts.append(_rel(resolved_path, root))
                did_write = True
            elif name == "screenshot":
                # The driver writes this file, so its parent must exist first.
                Path(resolved_path).parent.mkdir(parents=True, exist_ok=True)
                driver.screenshot(resolved_path)
                artifacts.append(_rel(resolved_path, root))

            proto.progress({"action": name, "op": op})
    except DriverError as exc:
        return _result_error(
            str(exc), final_response=_join_text(proto, texts), artifacts=artifacts
        )
    except Exception as exc:  # noqa: BLE001 — never leak a traceback to stdout
        return _result_error(
            f"browser operator failed: {type(exc).__name__}: {proto.redact(str(exc))}",
            final_response=_join_text(proto, texts),
            artifacts=artifacts,
        )
    finally:
        try:
            driver.close()
        except Exception:
            pass

    final = _join_text(proto, texts)
    if artifacts:
        final += "\n\nArtifacts: " + ", ".join(artifacts)
    result = {
        "status": "ok" if did_write else "no_changes",
        "final_response": final[:MAX_TEXT],
        "browser_artifacts": artifacts,
        "errors": [],
    }
    if any(a["action"] == "read" for a in actions) and all(
            a["action"] in {"navigate", "read"} for a in actions):
        result["mode"] = "browser_read"
        result["browser_sources"] = sources[:10]
    if session_ref:
        # Non-secret correlation id. The key deliberately avoids the substring
        # "session": redact_obj() treats any *session*-named key as sensitive
        # and would blank it, and this value must survive to the UI.
        result["browser_ref"] = session_ref
    return result


def preflight(task_text, allowlist) -> tuple[bool, str]:
    """Host-side preflight: may this browser task run at all?

    Parses the spec and checks every URL against the allowlist *before* the
    executor is spawned, so a blocked navigation never even starts a process.
    Returns ``(True, "")`` or ``(False, reason)``.
    """
    try:
        actions = parse_spec(task_text)
    except SpecError as exc:
        return False, str(exc)
    entries = [e for e in (normalize_host(a) for a in (allowlist or [])) if e]
    if not entries:
        return False, "no browser allowlist configured for this bot"
    for action in actions:
        if action["action"] in _URL_ACTIONS:
            allowed, reason = domain_allowed(action.get("url"), entries)
            if not allowed:
                return False, reason
    return True, ""


# ── Entry point ────────────────────────────────────────────────────────

def emit_result(result: dict) -> None:
    print(f"KYREX_RESULT_JSON:{json.dumps(redact_obj(result))}", flush=True)


def _workspace_root() -> Path:
    raw = (
        os.environ.get("KYREX_FS_ROOT")
        or os.environ.get("KYREX_BROWSER_ROOT")
        or DEFAULT_ROOT
    )
    return Path(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description="Kyrex Cloud Browser Operator")
    parser.add_argument("--task", required=True, help="JSON action spec")
    # run_task passes these to every executor; accept and ignore for uniformity.
    parser.add_argument("--base", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--repo-url", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--rift", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    root = _workspace_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        emit_result(_result_error(f"browser workspace unavailable: {exc}"))
        return

    allowlist = parse_allowlist(os.environ.get("KYREX_BROWSER_ALLOWLIST", ""))
    try:
        actions = parse_spec(args.task)
    except SpecError as exc:
        emit_result(_result_error(str(exc)))
        return

    kind = (os.environ.get("KYREX_BROWSER_DRIVER", "playwright").strip().lower()
            or "playwright")
    if kind not in ("playwright", "local"):
        emit_result(_result_error(f"unsupported KYREX_BROWSER_DRIVER: {kind!r}"))
        return

    bot_id = os.environ.get("KYREX_BOT_ID", "")
    owner = os.environ.get("KYREX_BOT_OWNER", "")
    # The host owns managed-session lifecycle. When it supplies a session
    # directory, that persistent browser profile IS the reconnect: the next
    # run for this (owner, bot) reuses it instead of starting cold. Without
    # one (a direct/standalone invocation) we fall back to the per-(bot,
    # owner) directory as before — no behaviour change for existing callers.
    session_ref = (os.environ.get("KYREX_BROWSER_SESSION_ID", "") or "").strip()
    managed_dir = (os.environ.get("KYREX_BROWSER_SESSION_DIR", "") or "").strip()
    session_dir = (Path(managed_dir) if managed_dir
                   else browser_session_dir(root, bot_id, owner))

    try:
        driver = build_driver(kind, session_dir, bot_id)
    except DriverError as exc:
        emit_result(_result_error(str(exc)))
        return

    emit_result(run_actions(actions, driver, root=root, allowlist=allowlist,
                            session_ref=session_ref))


if __name__ == "__main__":
    main()
