"""host_allowlist.py — host-side site allowlist + CDP redaction (Phase 2).

This is the HOST'S OWN copy of the safety checks, kept deliberately
independent of the executor's implementation. The Cloud is the policy
authority; these checks are DEFENSE IN DEPTH: if the Cloud's allowlist were
ever bypassed, misconfigured, or spoofed, the host still refuses to navigate
off-list. Two independent checks that must both pass is the point.

Everything here is pure, dependency-free, and side-effect-free so it can be
unit-tested without Chromium, Playwright, or a network.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

# A raw DevTools (CDP) endpoint hands over full control of the browser, so it
# is treated as a secret: scrubbed from every log line, error, and frame the
# host emits. The host never exposes CDP and never publishes such a URL.
_CDP_URL_RE = re.compile(
    r"(?i)\bwss?://[^\s\"'<>]*?(?:devtools|/json(?:/version)?)[^\s\"'<>]*"
)
_CDP_HTTP_URL_RE = re.compile(
    r"(?i)\bhttps?://[^\s\"'<>]*?(?:/devtools/|/json(?:/version)?)[^\s\"'<>]*"
)
_SECRET_KV_RE = re.compile(
    r"(?i)\b(secret|token|password|passwd|api[_-]?key|credential|authorization)"
    r"\b\s*[:=]\s*[^\s,;]+"
)

_SAFE_SCHEMES = ("http", "https")


def redact_text(text) -> str:
    """Scrub CDP endpoints and secret-shaped key/values from *text*."""
    if text is None:
        return ""
    out = str(text)
    out = _CDP_URL_RE.sub("[redacted-cdp]", out)
    out = _CDP_HTTP_URL_RE.sub("[redacted-cdp]", out)
    out = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}=[redacted]", out)
    return out


def redact_obj(obj):
    """Recursively redact a JSON-shaped object."""
    if isinstance(obj, dict):
        return {str(k): redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def contains_cdp_url(text) -> bool:
    """True if *text* carries a raw CDP/DevTools endpoint (must never ship)."""
    raw = str(text or "")
    return bool(_CDP_URL_RE.search(raw) or _CDP_HTTP_URL_RE.search(raw))


# ── Allowlist ─────────────────────────────────────────────────────────

def normalize_host(value) -> str | None:
    """Reduce an allowlist entry or URL to a bare lowercase hostname."""
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
    """Parse an allowlist from a JSON list, a comma-separated string, or a list."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
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
    """The bare host of an http(s) URL, or ``None`` (rejects userinfo/non-http)."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return None
    if parts.scheme not in _SAFE_SCHEMES:
        return None
    if parts.username or parts.password:
        return None
    return (parts.hostname or "").lower() or None


def domain_allowed(url, allowlist) -> tuple[bool, str]:
    """True iff *url*'s host is exactly, or a subdomain of, an allowlist entry."""
    host = host_of(url)
    if host is None:
        return False, "url must be an http(s) url with a host and no credentials"
    entries = [e for e in (normalize_host(x) for x in (allowlist or [])) if e]
    for entry in entries:
        if host == entry or host.endswith("." + entry):
            return True, ""
    return False, f"host {host!r} is not on this host's allowlist"


def effective_allowlist(host_allowlist, cloud_allowlist) -> list[str]:
    """The intersection of the host's and the Cloud's allowlists.

    Fail closed: when the host has its own list it is authoritative and the
    Cloud may only narrow it. When the host has none configured, the Cloud's
    list is adopted (the host trusts the Cloud's authority). When NEITHER is
    present the result is empty, which denies every navigation.
    """
    host_entries = [e for e in (normalize_host(x) for x in (host_allowlist or [])) if e]
    cloud_entries = [e for e in (normalize_host(x) for x in (cloud_allowlist or [])) if e]
    if not host_entries:
        return cloud_entries
    if not cloud_entries:
        return host_entries
    # An entry is kept when it is covered by the other side (exact or subdomain).
    def covered(entry, other):
        return any(entry == o or entry.endswith("." + o) or o.endswith("." + entry)
                   for o in other)

    return [e for e in host_entries if covered(e, cloud_entries)] or []


def _spec_urls(task_text):
    """Extract the navigate/download URLs from a browser task spec, or raise."""
    spec = json.loads(task_text)
    if not isinstance(spec, dict):
        raise ValueError("browser task must be a JSON object")
    if "actions" in spec:
        actions = spec["actions"]
        if not isinstance(actions, list) or not actions:
            raise ValueError("'actions' must be a non-empty list")
    elif spec.get("url"):
        actions = [{"action": "navigate", "url": spec["url"]}]
    else:
        raise ValueError("browser task requires 'actions' or a 'url'")
    urls = []
    for action in actions:
        if isinstance(action, dict) and str(action.get("action")) in (
            "navigate", "download"
        ):
            urls.append(action.get("url"))
    return urls


def preflight(task_text, allowlist) -> tuple[bool, str]:
    """Host-side preflight: may this task run at all, on THIS host?"""
    try:
        urls = _spec_urls(task_text)
    except (ValueError, json.JSONDecodeError, TypeError) as exc:
        return False, f"invalid browser task: {exc}"
    entries = [e for e in (normalize_host(x) for x in (allowlist or [])) if e]
    if not entries:
        return False, "no site allowlist configured on this host"
    for url in urls:
        allowed, reason = domain_allowed(url, entries)
        if not allowed:
            return False, reason
    return True, ""
