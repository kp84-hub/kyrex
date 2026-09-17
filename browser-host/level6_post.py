"""level6_post.py — the fixed-purpose Level 6 weekly Browser Host operation.

One job, no caller surface: navigate to the pinned Level 6 Training Facebook
PHOTOS page (``/level6training/photos``, NOT the timeline — the newest "THE
WEEKLY SIX" graphic appears as the FIRST photo there each week), prove the
final page really is that page, enumerate the recent VISIBLE photo posts in
deterministic NEWEST-FIRST DOM order, and — for each candidate,
up to a hard cap — capture the candidate (its primary post image, or the whole
article when there is no usable image) to a host-local temporary PNG, OCR that
PNG locally with the existing bounded Tesseract path, and delete the PNG again.
The FIRST/NEWEST candidate whose OCR carries exactly one ``THE WEEKLY SIX``
marker, exactly one printed ``WEEK OF MM.DD.YY`` label and six dated
Monday-Saturday rows is SELECTED; only its OCR text plus stable post metadata
travel back to the Cloud.

Why OCR drives discovery
------------------------
The ``THE WEEKLY SIX`` marker exists only as PIXELS inside the Facebook post
image — it is NOT in the caption/DOM text. Searching ``div[role="article"]``
visible text for the marker (the previous approach) therefore never matched,
so the validated date/Glofox pipeline was never reached. Discovery is now
purely: list recent visible post articles newest-first → OCR each → select the
newest well-formed Weekly Six. Caption/DOM text is never required and never
read.

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
* It never merges content across posts: exactly ONE candidate's OCR is
  returned, and the marker/label are required to occur exactly once in it.

Fail-closed guarantees
----------------------
Every bound is INTERNAL and hard: candidate count (``MAX_CANDIDATES``),
scrolling (``MAX_SCROLLS``), OCR time (``OCR_TIMEOUT``) and OCR output
(``MAX_OCR_BYTES``). Every candidate PNG is deleted on success AND on every
failure path (``try/finally``). The operation refuses (with a structured,
non-secret ``error_code``) when there is no weekly candidate, when the page
identity changed, when the feed order cannot be trusted, when a capture or OCR
fails, or when the newest credible candidate is ambiguous/malformed — it NEVER
silently falls through to an older valid post over a newer malformed Weekly
Six candidate.
"""
from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
from datetime import date, timedelta
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

# ── Hard internal bounds (never caller-controlled) ─────────────────────
#: At most this many NEWEST visible candidates are captured + OCR'd.
MAX_CANDIDATES = 6

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
CONVERT_BIN_ENV = "KYREX_IMAGEMAGICK_BIN"
DEFAULT_CONVERT_BIN = "convert"
OCR_SCALE = "300%"
OCR_PASS_TIMEOUT = 12.0

#: The six class days, in order. Sunday is never part of the week.
WEEKDAYS: tuple[str, ...] = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
)

#: Printed week label: ``WEEK OF 09.21.26`` / ``WEEK OF 9.21.2026``.
_WEEK_LABEL_RE = re.compile(
    r"^week\s*of\s*(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2}|\d{4})\b",
    re.IGNORECASE,
)

#: A dated workout row: ``MONDAY 09.21 Back Squat`` (year optional).
_ROW_RE = re.compile(
    r"^(?P<weekday>[A-Za-z]{3,9})\.?\s+"
    r"(?P<month>\d{1,2})[.\-/](?P<day>\d{1,2})"
    r"(?:[.\-/](?P<year>\d{2}|\d{4}))?\b",
    re.IGNORECASE,
)

_LOOSE_WEEK_LABEL_RE = re.compile(
    # Sparse OCR can place the stylised heading between ``WEEK OF`` and its
    # small date. The bounded non-digit gap accepts that layout but cannot
    # drift into workout-row dates farther down the image.
    r"week\s*of.{0,120}?(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2}|\d{4})\b",
    re.IGNORECASE,
)
_ARROW_ROW_RE = re.compile(r"(?:>{2,}|>»|»)\s*(?P<workout>.+)$")
_WORKOUT_TRAILING_NOISE = " \t-–—:|.<>»«=~_©®™•·‘’“”'\""


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


def _run_preprocess(source, target, *, convert_bin=None,
                    timeout: float = OCR_TIMEOUT) -> None:
    """Create one host-local OCR image using a fixed ImageMagick argv."""
    binary = str(convert_bin or os.environ.get(CONVERT_BIN_ENV)
                 or DEFAULT_CONVERT_BIN)
    command = [
        binary, str(source), "-resize", OCR_SCALE,
        "-colorspace", "Gray", "-contrast-stretch", "1%x1%", str(target),
    ]
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                start_new_session=True)
    except (FileNotFoundError, OSError) as exc:
        raise Level6OcrError(
            "ocr_unavailable",
            f"the OCR preprocessor is unavailable ({type(exc).__name__})",
        ) from exc
    try:
        proc.communicate(timeout=float(timeout))
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(proc)
        raise Level6OcrError(
            "ocr_timeout", "the OCR preprocessor exceeded its time budget"
        ) from exc
    if proc.returncode != 0:
        raise Level6OcrError(
            "ocr_failed",
            f"the OCR preprocessor exited with code {proc.returncode}",
        )


def _marker_line_count(text: str) -> int:
    """Count heading-like lines despite stylised ``SIX`` OCR corruption."""
    count = 0
    for line in str(text or "").splitlines():
        compact = re.sub(r"[^A-Z0-9]", "", line.upper())
        # Observed output from the published graphic includes THEWEEKLYS and
        # THEWEEKLYSS. The explicit week + six-row checks remain mandatory.
        if compact.startswith("THEWEEKLY") and len(compact) <= 24:
            count += 1
    return count


def _printed_week(block_text: str, sparse_text: str) -> date:
    """Read one explicit WEEK OF date, accepting a line break before it."""
    labels = []
    for text in (block_text, sparse_text):
        flattened = re.sub(r"\s+", " ", str(text or ""))
        labels.extend(_LOOSE_WEEK_LABEL_RE.findall(flattened))
    unique = set(labels)
    if len(unique) != 1:
        code = "ambiguous" if len(unique) > 1 else "malformed_newest"
        raise Level6OcrError(code, "the weekly image has no unique week label")
    month, day_number, year_raw = next(iter(unique))
    year = int(year_raw) + (2000 if len(year_raw) == 2 else 0)
    try:
        monday = date(year, int(month), int(day_number))
    except ValueError as exc:
        raise Level6OcrError(
            "malformed_newest", "the weekly image has an invalid week label"
        ) from exc
    if monday.weekday() != 0:
        raise Level6OcrError(
            "malformed_newest", "the weekly image week label is not a Monday"
        )
    return monday


def _ordered_workouts(block_text: str) -> list[str]:
    """Extract the six right-column workout names from the fixed graphic."""
    workouts = []
    for raw in str(block_text or "").splitlines():
        match = _ARROW_ROW_RE.search(raw)
        if not match:
            continue
        workout = match.group("workout").strip(_WORKOUT_TRAILING_NOISE)
        workout = re.sub(r"\s+", " ", workout).strip()
        workout = re.sub(r"[^\w&+%)]*$", "", workout).strip()
        if workout:
            workouts.append(workout)
    if len(workouts) != len(WEEKDAYS):
        raise Level6OcrError(
            "malformed_newest",
            "the weekly image does not contain exactly six workout rows",
        )
    return workouts


def canonicalize_weekly_ocr(block_text: str, sparse_text: str) -> str:
    """Convert two bounded OCR layouts into the strict Cloud text contract."""
    marker_counts = (
        _marker_line_count(block_text), _marker_line_count(sparse_text)
    )
    if max(marker_counts) == 0:
        raise Level6OcrError("marker_absent", "the weekly marker was not found")
    if max(marker_counts) > 1:
        raise Level6OcrError("ambiguous", "the weekly marker is ambiguous")
    monday = _printed_week(block_text, sparse_text)
    workouts = _ordered_workouts(block_text)
    lines = ["THE WEEKLY SIX", f"WEEK OF {monday:%m.%d.%y}"]
    for offset, (weekday, workout) in enumerate(zip(WEEKDAYS, workouts)):
        day_value = monday + timedelta(days=offset)
        lines.append(f"{weekday} {day_value:%m.%d} {workout}")
    return "\n".join(lines)


def run_weekly_ocr(png_path, *, convert_bin=None,
                   tesseract_bin=None) -> tuple[str, bool]:
    """Preprocess once, run two fixed OCR layouts, return canonical text."""
    prepared = str(Path(png_path).with_suffix(".ocr.png"))
    try:
        _run_preprocess(png_path, prepared, convert_bin=convert_bin)
        block, block_truncated = run_ocr(
            prepared, tesseract_bin=tesseract_bin, psm="6",
            timeout=OCR_PASS_TIMEOUT,
        )
        sparse, sparse_truncated = run_ocr(
            prepared, tesseract_bin=tesseract_bin, psm="11",
            timeout=OCR_PASS_TIMEOUT,
        )
        if block_truncated or sparse_truncated:
            return "", True
        return canonicalize_weekly_ocr(block, sparse), False
    finally:
        _cleanup(prepared)


# ── OCR classification: is this candidate a well-formed Weekly Six? ────

def _weekday_name(token: str) -> str | None:
    """Normalise a weekday token (full name or abbreviation) or ``None``."""
    raw = str(token or "").strip().lower().rstrip(".")
    if len(raw) < 3:
        return None
    for name in WEEKDAYS:
        lower = name.lower()
        if raw == lower or lower.startswith(raw) or raw.startswith(lower[:3]):
            return name
    return None


def analyze_ocr_text(text, *, truncated: bool = False) -> tuple[str, str]:
    """Classify ONE candidate's OCR text. Returns ``(verdict, detail)``.

    ``verdict`` is one of:

    * ``"truncated"``  — the OCR output was capped, so it cannot be trusted;
    * ``"absent"``     — no marker at all: NOT a Weekly Six candidate (skip);
    * ``"ambiguous"``  — the marker/``WEEK OF`` label occurs more than once, so
      it reads as content from more than one post (fail closed);
    * ``"malformed"``  — the marker is present but the post is not a
      well-formed Weekly Six (missing label, or not exactly six dated
      Monday-Saturday rows);
    * ``"valid"``      — exactly one marker, exactly one printed
      ``WEEK OF MM.DD.YY`` label, and six dated Monday-Saturday rows.

    ``detail`` is a short, non-secret reason (never a path or image data). This
    is a SELECTION check only — the Cloud's ``parse_ocr_text`` remains the
    authority that validates the dates and extracts the workouts.
    """
    if truncated:
        return "truncated", "the OCR output was truncated"
    if not isinstance(text, str) or not text.strip():
        return "absent", ""

    marker_count = text.lower().count(MARKER.lower())
    if marker_count == 0:
        return "absent", ""
    if marker_count > 1:
        return "ambiguous", (
            f"the captured text contains {marker_count} {MARKER} markers"
        )

    lines = [line.strip() for line in text.splitlines()]
    labels = [line for line in lines if _WEEK_LABEL_RE.match(line)]
    if len(labels) > 1:
        return "ambiguous", (
            f"the captured text contains {len(labels)} WEEK OF labels"
        )
    if not labels:
        return "malformed", "the captured text has no WEEK OF MM.DD.YY label"

    rows: list[str] = []
    for line in lines:
        match = _ROW_RE.match(line)
        if not match:
            continue
        weekday = _weekday_name(match.group("weekday"))
        if weekday is None:
            continue
        rows.append(weekday)
    if len(rows) != len(WEEKDAYS):
        return "malformed", (
            f"the captured text states {len(rows)} dated weekday rows "
            f"(expected {len(WEEKDAYS)})"
        )
    if len(set(rows)) != len(WEEKDAYS) or set(rows) != set(WEEKDAYS):
        return "malformed", (
            "the captured text does not state each of Monday-Saturday "
            "exactly once"
        )
    return "valid", ""


# ── Screenshot bookkeeping (host-local only) ───────────────────────────

def _post_ref(candidate: dict) -> str:
    """Stable, non-secret identity for a candidate post (content-derived)."""
    raw = "{}\n{}".format(candidate.get("permalink") or "",
                          candidate.get("index"))
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
                      ocr_runner=None, max_candidates: int = MAX_CANDIDATES,
                      max_scrolls: int = MAX_SCROLLS) -> dict:
    """Run the fixed-purpose Level 6 weekly capture. Returns a result dict.

    Announces (and requires permission for) exactly the read-only operations
    the dedicated policy grants: ``browser.navigate`` on the pinned page,
    ``browser.read`` while listing recent visible posts, and one
    ``browser.screenshot`` per candidate captured. A denial at any point stops
    immediately with a fail-closed result — no later operation is announced or
    performed. ``max_candidates``/``max_scrolls`` are INTERNAL test seams and
    are never read from the task spec.
    """
    ocr_runner = ocr_runner or run_weekly_ocr
    cap = max(1, int(max_candidates))

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

    # 2. List visible Photos-tab image elements, newest-first. This stays in
    # the already-authorized browser driver; no raw image URL is fetched.
    if not proto.operation("browser.read", PAGE_URL,
                           f"list recent visible posts for {MARKER}", ""):
        return _result_error("read_denied", "browser.read denied")
    try:
        candidates = driver.scan_level6_photos(max_candidates=cap)
    except _bo.DriverError as exc:
        code = getattr(exc, "code", "") or "locate_failed"
        return _result_error(
            code, f"post lookup failed ({type(exc).__name__})"
        )
    except Exception as exc:  # noqa: BLE001 — fail closed
        return _result_error(
            "locate_failed", f"photos_list:{type(exc).__name__}"
        )
    candidates = [c for c in (candidates or []) if isinstance(c, dict)]
    if not candidates:
        return _result_error(
            "no_candidate",
            f"weekly post not available: no recent visible {MARKER} post "
            "was found",
        )

    # 3. Walk candidates newest-first, capturing + OCR'ing each (bounded), and
    #    select the FIRST well-formed Weekly Six. Any credible-but-unusable
    #    NEWER candidate is refused outright — never skipped for an older one.
    scanned = 0
    for position, candidate in enumerate(candidates[:cap]):
        descriptor = {
            "index": candidate.get("index"),
            "permalink": PAGE_URL,
            "key": str(candidate.get("key") or ""),
        }
        ref = _post_ref(descriptor)

        # Capture ONLY this candidate element (announced, then performed).
        if not proto.operation("browser.screenshot", ref,
                               "capture a candidate post", ""):
            return _result_error("screenshot_denied", "browser.screenshot denied")
        png = _png_path(root, ref)
        try:
            Path(png).parent.mkdir(parents=True, exist_ok=True)
            driver.capture_level6_photo(descriptor, png)
        except Exception as exc:  # noqa: BLE001 — fail closed
            _cleanup(png)
            return _result_error(
                "capture_failed", f"post capture failed ({type(exc).__name__})"
            )
        if not os.path.isfile(png):
            _cleanup(png)
            return _result_error(
                "capture_missing", "the post capture produced no image"
            )
        scanned = position + 1

        # OCR locally, then delete the PNG — always, including on failure.
        try:
            text, truncated = ocr_runner(png)
        except Level6OcrError as exc:
            if exc.code == "marker_absent":
                # This recent post is not a Weekly Six; continue newest-first.
                continue
            return _result_error(exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 — fail closed
            return _result_error(
                "ocr_failed", f"OCR failed ({type(exc).__name__})"
            )
        finally:
            _cleanup(png)

        verdict, detail = analyze_ocr_text(text, truncated=truncated)
        if verdict == "truncated":
            return _result_error(
                "ocr_truncated",
                f"weekly post not available: {detail}",
            )
        if verdict == "absent":
            # Not a Weekly Six post at all — a newer post may still be one.
            continue
        if verdict == "ambiguous":
            return _result_error(
                "ambiguous",
                f"weekly post not available: the newest credible {MARKER} "
                f"post is ambiguous ({detail})",
            )
        if verdict == "malformed":
            return _result_error(
                "malformed_newest",
                f"weekly post not available: the newest {MARKER} post is not "
                f"well formed ({detail})",
            )

        # verdict == "valid": this is the newest well-formed Weekly Six.
        return {
            "status": "no_changes",
            "final_response": "",
            "browser_artifacts": [],
            "errors": [],
            "level6_weekly": {
                "marker": MARKER,
                "page_url": PAGE_URL,
                "post_ref": ref,
                "permalink": descriptor["permalink"],
                "scan_index": descriptor["index"],
                "candidates_scanned": scanned,
                "ocr_engine": OCR_ENGINE,
                "ocr_text": text[:MAX_OCR_TEXT],
                "ocr_truncated": False,
            },
        }

    return _result_error(
        "no_candidate",
        f"weekly post not available: none of the {scanned} recent visible "
        f"posts was a {MARKER} post",
    )
