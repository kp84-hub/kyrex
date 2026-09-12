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
    managed CDP endpoint (``KYREX_BROWSER_WS_ENDPOINTS_JSON``).
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
import html
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

MAX_TEXT = 4000
DEFAULT_ROOT = "/tmp/kyrex-browser"

VALID_ACTIONS = frozenset(
    {"navigate", "read", "click", "type", "upload", "download", "screenshot", "delete"}
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
    """The requested browser transport is unavailable or unusable."""


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


def is_consequential(*parts) -> bool:
    """True if any word in *parts* is a consequential verb."""
    words = " ".join(str(p or "") for p in parts).lower()
    return any(re.search(rf"\b{re.escape(verb)}\b", words) for verb in CONSEQUENTIAL_VERBS)


def action_operation(action: dict) -> str:
    """Map a structured action to its host operation (dotted form)."""
    name = action["action"]
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
        self.progress_notes: list[dict] = []

    def redact(self, text) -> str:
        return redact(text)

    def progress(self, note: dict) -> None:
        self.progress_notes.append(dict(note))

    def operation(self, op: str, target, summary: str, detail=None) -> bool:
        self.operations.append(op)
        if self.allow is not None and op not in self.allow:
            return False
        return True


# ── Drivers ────────────────────────────────────────────────────────────

def _strip_html(body: str) -> str:
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", body or "")
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", html.unescape(body)).strip()


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

    def close(self) -> None:
        return None


class PlaywrightDriver:
    """Real Chromium transport (default). Lazy-imports Playwright."""

    def __init__(self, session_dir: Path, endpoint: str = ""):
        self.session_dir = Path(session_dir)
        self.endpoint = (endpoint or "").strip()
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
        self._pw = sync_playwright().start()
        if self.endpoint:
            self._browser = self._pw.chromium.connect_over_cdp(self.endpoint)
            self._context = self._browser.new_context()
        else:
            self._browser = self._pw.chromium.launch(
                headless=True,
                executable_path=os.environ.get(
                    "KYREX_BROWSER_EXECUTABLE", "/usr/bin/chromium"
                ),
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

    def close(self) -> None:
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


def build_driver(kind: str, session_dir, bot_id: str):
    if kind == "local":
        return LocalDriver(session_dir)
    return PlaywrightDriver(session_dir, endpoint=managed_endpoint(bot_id))


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


def run_actions(actions, driver, *, root, allowlist, proto=None) -> dict:
    """Execute a validated action list, returning the executor result dict.

    Enforces the allowlist on every URL and the workspace confinement on every
    path *before* requesting approval, then performs each action only after the
    host (and, when required, the operator) permits it. Any denial stops the
    run immediately with a result — never a hang.
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
    did_write = False

    try:
        driver.open()
    except DriverError as exc:
        return _result_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _result_error(
            f"browser operator failed to start: {type(exc).__name__}: {proto.redact(str(exc))}"
        )

    try:
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
                texts.append(_safe_text(proto, driver.text()))
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
    return {
        "status": "ok" if did_write else "no_changes",
        "final_response": final[:MAX_TEXT],
        "browser_artifacts": artifacts,
        "errors": [],
    }


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
        if action["action"] in ("navigate", "download"):
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
    session_dir = browser_session_dir(root, bot_id, owner)

    try:
        driver = build_driver(kind, session_dir, bot_id)
    except DriverError as exc:
        emit_result(_result_error(str(exc)))
        return

    emit_result(run_actions(actions, driver, root=root, allowlist=allowlist))


if __name__ == "__main__":
    main()
