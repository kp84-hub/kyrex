"""level6_post.py — the fixed-purpose Level 6 weekly Browser Host operation.

One job, no caller surface: navigate to the pinned Level 6 Training Facebook
page, prove the final page really is that page, locate the ONE newest visible
feed post carrying the ``THE WEEKLY SIX`` marker (bounded internal scrolling
for lazy loading), capture ONLY that post element to a host-local PNG, OCR
that PNG locally with Tesseract under a bounded timeout and output cap, delete
the PNG, and return the structured OCR text plus stable post metadata.

What this module never does
---------------------------
* It never reads a URL from the task spec — the page is the pinned
  ``browser_operator.LEVEL6_PAGE_URL`` constant and nothing else.
* It never clicks, types, submits, uploads, downloads, or deletes. Scrolling
  is internal (``page.mouse.wheel``) and bounded; there is no generic
  interaction capability here.
* It never returns PNG bytes or a PNG path to the Cloud: the result's
  ``browser_artifacts`` is EMPTY, ``final_response`` is empty, and only OCR
  text + non-secret post metadata travel.
* It never accepts content collected across posts: it returns the FIRST
  successful scan's credible posts and the caller refuses anything but
  exactly one.

Failure handling is explicit: every failure returns a structured
``error_code`` (``post_not_available`` for a not-yet-published week), and OCR
failures/timeouts/oversize output raise :class:`Level6OcrError`.
"""
from __future__ import annotations

import hashlib
import os
import signal
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

# ``browser_operator.py`` ships next to this module in the Browser Host image
# (and lives in kyrex-cloud/ in the repo), so the pinned page, the marker, and
# the allowlist helper are single-sourced there.
import browser_operator as _bo

PAGE_URL = _bo.LEVEL6_PAGE_URL
MARKER = _bo.LEVEL6_POST_MARKER

#: Path segment that identifies the Level 6 Training page (page identity check).
PAGE_SEGMENT = "level6training"

#: Final-page paths that are explicitly NOT the Level 6 page.
_REJECT_PATH_SEGMENTS = (
    "/login", "/checkpoint", "/consent", "/recover", "/privacy", "/help",
    "/settings", "/policies",
)

#: Accepted Facebook origins for the FINAL page (origin validation).
_FACEBOOK_ORIGINS = frozenset({"www.facebook.com", "facebook.com",
                               "web.facebook.com"})

#: Host-local screenshot directory, relative to the operator workspace root.
SCREENSHOT_DIR = "browser-artifacts"

#: Bounded post-selection scrolling (internal; never a generic action).
MAX_SCROLLS = 8

#: OCR engine + hard bounds.
OCR_ENGINE = "tesseract"
OCR_BIN_ENV = "KYREX_TESSERACT_BIN"
DEFAULT_OCR_BIN = "tesseract"
OCR_LANG = "eng"
OCR_PSM = "6"
OCR_TIMEOUT = 25.0
MAX_OCR_BYTES = 48_000
MAX_OCR_TEXT = MAX_OCR_BYTES


class Level6OcrError(Exception):
    """OCR could not produce trustworthy text (fail closed)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)


# ── Result shapes ──────────────────────────────────────────────────────

def _result_error(code: str, message: str) -> dict:
    """A structured fail-closed result. Carries NO path and NO image data."""
    return {
        "status": "error",
        "final_response": "",
        "browser_artifacts": [],
        "errors": [str(message)],
        "level6_weekly": {
            "error_code": str(code),
            "marker": MARKER,
            "page_url": PAGE_URL,
        },
    }


# ── Page identity ──────────────────────────────────────────────────────

def page_identity_ok(url) -> tuple[bool, str]:
    """Validate the FINAL page's origin AND Level 6 page identity.

    Returns ``(True, "")`` only for an https URL on a Facebook origin whose
    path is the Level 6 Training page. A login/consent/checkpoint surface, a
    non-Facebook origin, a non-https URL, or any other page is rejected.
    """
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return False, "the final page URL is unparseable"
    if parts.scheme != "https":
        return False, "the final page is not served over https"
    host = (parts.hostname or "").lower()
    if host not in _FACEBOOK_ORIGINS:
        return False, f"the final page is not a Facebook origin ({host or 'none'})"
    path = (parts.path or "/").lower()
    for segment in _REJECT_PATH_SEGMENTS:
        if path.startswith(segment) or segment + "/" in path:
            return False, "the final page is a Facebook login/consent surface"
    # Require the EXACT page: the FIRST path segment must be precisely
    # ``level6training`` (``/level6training`` or ``/level6training/...``), so a
    # look-alike path that merely shares that prefix (e.g. ``/level6trainingXYZ``
    # or ``/level6trainingfake``) is rejected rather than accepted.
    if path.lstrip("/").split("/", 1)[0] != PAGE_SEGMENT:
        return False, "the final page is not the Level 6 Training page"
    return True, ""


# ── OCR (local subprocess, bounded) ────────────────────────────────────

def _kill_process_group(proc) -> None:
    """Kill *proc* and its whole process group, then reap it (never raises)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 — fall back to the direct child
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.communicate(timeout=2)
    except Exception:  # noqa: BLE001 — reap best-effort, never mask
        pass


def run_ocr(png_path, *, tesseract_bin: str | None = None,
            lang: str = OCR_LANG, psm: str = OCR_PSM,
            timeout: float = OCR_TIMEOUT,
            max_bytes: int = MAX_OCR_BYTES) -> tuple[str, bool]:
    """OCR *png_path* locally, returning ``(text, truncated)``.

    Explicit, bounded failure handling:

    * the engine is invoked with a hard ``timeout`` — on expiry the child is
      KILLED and :class:`Level6OcrError` (``ocr_timeout``) is raised;
    * stdout is capped at ``max_bytes``; anything larger is reported as
      ``truncated=True`` (the caller rejects truncated OCR as untrustworthy);
    * a missing engine (``ocr_unavailable``) or a non-zero exit
      (``ocr_failed``) raise rather than returning partial text.
    """
    binary = str(tesseract_bin or os.environ.get(OCR_BIN_ENV)
                 or DEFAULT_OCR_BIN)
    command = [binary, str(png_path), "stdout", "-l", str(lang),
               "--psm", str(psm)]
    try:
        # Own process GROUP: a timeout must kill the engine AND anything it
        # spawned, otherwise an orphan holding the stdout pipe would keep the
        # bounded wait from actually returning.
        proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                start_new_session=True)
    except (FileNotFoundError, OSError) as exc:
        raise Level6OcrError(
            "ocr_unavailable",
            f"the OCR engine is unavailable ({type(exc).__name__})",
        ) from exc
    try:
        out, _err = proc.communicate(timeout=float(timeout))
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(proc)
        raise Level6OcrError(
            "ocr_timeout", "the OCR engine exceeded its time budget"
        ) from exc
    if proc.returncode != 0:
        raise Level6OcrError(
            "ocr_failed", f"the OCR engine exited with code {proc.returncode}"
        )
    truncated = len(out) > int(max_bytes)
    return out[:int(max_bytes)].decode("utf-8", "replace"), truncated


# ── Screenshot bookkeeping (host-local only) ───────────────────────────

def _post_ref(post: dict) -> str:
    """Stable, non-secret identity for the located post (content-derived)."""
    raw = "{}\n{}".format(post.get("permalink") or "", post.get("text") or "")
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _png_path(root, ref: str) -> str:
    return str(Path(root) / SCREENSHOT_DIR / f"level6-weekly-{ref}.png")


def _cleanup(path) -> None:
    """Delete a host-local artifact. Best-effort, never raises."""
    try:
        os.remove(str(path))
    except FileNotFoundError:
        pass
    except OSError:
        pass


# ── The operation ──────────────────────────────────────────────────────

def run_level6_weekly(driver, proto, *, root, allowlist,
                      ocr_runner=None, max_scrolls: int = MAX_SCROLLS) -> dict:
    """Run the fixed-purpose Level 6 weekly capture. Returns a result dict.

    Announces (and requires permission for) exactly the three read-only
    operations the dedicated policy grants, in order: ``browser.navigate`` on
    the pinned page, ``browser.read`` while locating the post, and
    ``browser.screenshot`` for the post-element capture. A denial at any point
    stops immediately with a fail-closed result — no later operation is
    announced or performed.
    """
    ocr_runner = ocr_runner or (lambda path: run_ocr(path))

    # 0. Defense in depth: the pinned page must be allowlisted for this task.
    allowed, reason = _bo.domain_allowed(PAGE_URL, allowlist or [])
    if not allowed:
        return _result_error("not_allowlisted", reason)

    # 1. Navigate (fixed URL — never read from the spec).
    if not proto.operation("browser.navigate", PAGE_URL,
                           f"navigate to {PAGE_URL}", ""):
        return _result_error("navigate_denied", "browser.navigate denied")
    try:
        driver.navigate(PAGE_URL)
    except Exception as exc:  # noqa: BLE001 — fail closed
        return _result_error(
            "navigate_failed", f"navigation failed ({type(exc).__name__})"
        )

    ok, reason = page_identity_ok(driver.current_url())
    if not ok:
        return _result_error("page_identity", reason)

    # 2. Locate the ONE newest credible post (bounded internal scrolling).
    if not proto.operation("browser.read", PAGE_URL,
                           f"locate the newest {MARKER} post", ""):
        return _result_error("read_denied", "browser.read denied")
    try:
        found = driver.query_level6_posts(MARKER, max_scrolls=max_scrolls)
    except Exception as exc:  # noqa: BLE001 — fail closed
        return _result_error(
            "locate_failed", f"post lookup failed ({type(exc).__name__})"
        )
    posts = [p for p in (found or []) if isinstance(p, dict)]
    if not posts:
        return _result_error(
            "post_not_available",
            f"weekly post not available: no visible {MARKER} post",
        )
    if len(posts) > 1:
        return _result_error(
            "ambiguous_posts",
            f"weekly post not available: {len(posts)} visible {MARKER} posts "
            "are credible (ambiguous)",
        )
    post = posts[0]
    ref = _post_ref(post)

    # 3. Capture ONLY the located post element (announced, then performed).
    if not proto.operation("browser.screenshot", ref,
                           "capture the located post element", ""):
        return _result_error("screenshot_denied", "browser.screenshot denied")
    png = _png_path(root, ref)
    try:
        Path(png).parent.mkdir(parents=True, exist_ok=True)
        driver.screenshot_element(post, png)
    except Exception as exc:  # noqa: BLE001 — fail closed
        _cleanup(png)
        return _result_error(
            "capture_failed", f"post capture failed ({type(exc).__name__})"
        )
    if not os.path.isfile(png):
        return _result_error(
            "capture_missing", "the post capture produced no image"
        )

    # 4. OCR locally, then delete the PNG — always, including on failure.
    try:
        text, truncated = ocr_runner(png)
    except Level6OcrError as exc:
        return _result_error(exc.code, str(exc))
    except Exception as exc:  # noqa: BLE001 — fail closed
        return _result_error(
            "ocr_failed", f"OCR failed ({type(exc).__name__})"
        )
    finally:
        _cleanup(png)

    if not isinstance(text, str) or not text.strip():
        return _result_error(
            "ocr_empty", "OCR produced no text for the captured post"
        )

    return {
        "status": "no_changes",
        "final_response": "",
        "browser_artifacts": [],
        "errors": [],
        "level6_weekly": {
            "marker": MARKER,
            "page_url": PAGE_URL,
            "post_ref": ref,
            "permalink": str(post.get("permalink") or ""),
            "ocr_engine": OCR_ENGINE,
            "ocr_text": text[:MAX_OCR_TEXT],
            "ocr_truncated": bool(truncated),
        },
    }
