#!/usr/bin/env python3
"""Focused tests for the Browser Host Level 6 weekly operation.

Covers, against the REAL production code (no browser, no network):

  1. final-page identity validation — Facebook origin AND Level 6 page, with
     login/consent/other-origin/non-https rejected;
  2. the local OCR SUBPROCESS boundary — a real ``subprocess`` invocation
     against stub executables: success, non-zero exit, oversize output
     (truncated), hard timeout (child killed, bounded), and a missing engine;
  3. OCR-driven candidate discovery — the marker lives ONLY in the post image,
     so listing is CONTENT-BLIND: it never reads caption/DOM text and never
     requires the marker there. Newest-first: for each candidate the operation
     captures it, OCRs locally, and selects the FIRST/NEWEST well-formed
     Weekly Six. Bounded candidate count + bounded scrolling are enforced;
  4. malformed/ambiguous NEWER posts are refused — the operation never silently
     falls through to an older valid post;
  5. cleanup — every candidate PNG is deleted on success AND on every failure;
  6. the response contract — structured OCR text + post metadata only; no PNG
     bytes, no PNG path, no ``browser_artifacts``, no ``final_response`` text;
  7. explicit, DISTINCT failure codes — no_candidate, ambiguous,
     malformed_newest, ordering_untrusted, page_identity, capture_failed, the
     ``ocr_*`` set, and per-operation denials that stop before any later
     operation is announced;
  8. the real ``PlaywrightDriver.capture_level6_candidate`` prefers the largest
     visible post IMAGE and falls back to the article element.

Run: python3 test_level6_post.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(HERE), str(REPO / "kyrex-cloud")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import browser_operator as bo  # noqa: E402 — production operator/constants
import level6_post as l6  # noqa: E402 — the host operation under test

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def expect_code(name, fn, code, *args, **kwargs):
    """Expect a Level6OcrError carrying *code*."""
    try:
        fn(*args, **kwargs)
    except l6.Level6OcrError as exc:
        check(name, exc.code == code, f"code={exc.code!r} expected {code!r}")
        return
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"wrong error {type(exc).__name__}: {exc}")
        return
    check(name, False, "expected Level6OcrError, none raised")


PAGE = bo.LEVEL6_PAGE_URL
MARKER = bo.LEVEL6_POST_MARKER

#: A realistic, well-formed Weekly Six post as the OCR of its IMAGE would read.
VALID_OCR = "\n".join([
    "Level 6 Training",
    "THE WEEKLY SIX",
    "WEEK OF 09.21.26",
    "MONDAY 09.21 Back Squat",
    "TUESDAY 09.22 Front Squat",
    "WEDNESDAY 09.23 Deadlift",
    "THURSDAY 09.24 Bench Press",
    "FRIDAY 09.25 Clean & Jerk",
    "SATURDAY 09.26 Snatch",
])

#: The marker present but a malformed week (only five dated rows).
MALFORMED_OCR = "\n".join([
    "THE WEEKLY SIX",
    "WEEK OF 09.21.26",
    "MONDAY 09.21 Back Squat",
    "TUESDAY 09.22 Front Squat",
    "WEDNESDAY 09.23 Deadlift",
    "THURSDAY 09.24 Bench Press",
    "FRIDAY 09.25 Clean & Jerk",
])

#: The marker present but the marker twice (content from >1 post): ambiguous.
AMBIGUOUS_OCR = VALID_OCR + "\nTHE WEEKLY SIX"

#: A perfectly ordinary non-Weekly-Six post caption.
ORDINARY_OCR = "Level 6 Training\nNew class times posted, see you on the floor!"

CAND0 = {"index": 0, "permalink": "https://www.facebook.com/level6training/posts/1"}
CAND1 = {"index": 1, "permalink": "https://www.facebook.com/level6training/posts/2"}
CAND2 = {"index": 2, "permalink": "https://www.facebook.com/level6training/posts/3"}


# ══ 1. final-page identity ════════════════════════════════════════════

print("\nTest 1: final Facebook origin + Level 6 page identity")

# The pinned page is now the Level 6 PHOTOS tab (the newest "THE WEEKLY SIX"
# graphic appears as the FIRST photo there each week), NOT the timeline. Page
# identity is about the FINAL page being the Level 6 page at all, so the photos
# tab, a post under it, and a canonical variant are accepted, while any other
# page/origin is rejected.
check("the pinned page is the Level 6 photos tab (not the timeline)",
      PAGE == "https://www.facebook.com/level6training/photos", f"PAGE={PAGE!r}")

for url in (PAGE,
            "https://www.facebook.com/level6training/photos",
            "https://www.facebook.com/level6training/photos/",
            "https://www.facebook.com/level6training/photos?ref=page_internal",
            "https://www.facebook.com/level6training/posts/1234"):
    check(f"accepted: {url}", l6.page_identity_ok(url)[0] is True,
          f"{l6.page_identity_ok(url)}")

for url, why in (
    ("", "empty"),
    ("http://www.facebook.com/level6training/", "non-https"),
    ("https://evil.example/level6training/", "foreign origin"),
    ("https://www.facebook.com.evil.example/level6training/", "suffix spoof"),
    ("https://www.facebook.com/login/?next=%2Flevel6training%2F", "login wall"),
    ("https://www.facebook.com/checkpoint/?next=/level6training/", "checkpoint"),
    ("https://www.facebook.com/someotherpage/", "wrong page"),
    ("https://www.facebook.com/someotherpage/photos", "arbitrary page"),
    ("https://example.com/level6training/photos", "arbitrary origin"),
    ("https://www.facebook.com/level6trainingfake/", "look-alike path prefix"),
    ("https://www.facebook.com/level6trainingXYZ/posts/1", "look-alike prefix"),
    ("https://www.facebook.com/level6training-events/photos", "look-alike suffix"),
):
    check(f"rejected ({why}): {url!r}", l6.page_identity_ok(url)[0] is False,
          f"{l6.page_identity_ok(url)}")


# ══ 2. the OCR subprocess boundary ════════════════════════════════════

print("\nTest 2: local OCR subprocess boundary (real subprocess)")


def _stub(tmp, name, body):
    path = Path(tmp) / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return str(path)


with tempfile.TemporaryDirectory(prefix="l6-ocr-") as tmp:
    png = Path(tmp) / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    ok = _stub(tmp, "tess_ok", 'printf "WEEK OF 09.21.26\\nMONDAY 09.21 Squat\\n"')
    text, truncated = l6.run_ocr(png, tesseract_bin=ok)
    check("stub OCR returns its text", text.startswith("WEEK OF 09.21.26"),
          f"text={text!r}")
    check("small output is not truncated", truncated is False)

    # A stub that echoes its argv proves the engine is handed the PNG path as
    # its first positional argument (with the FIXED language/psm flags after).
    argv_stub = _stub(tmp, "tess_argv", 'printf "%s\\n" "$@"')
    argv_text, _ = l6.run_ocr(png, tesseract_bin=argv_stub)
    argv = argv_text.splitlines()
    check("the OCR engine receives the PNG path as its first argument",
          argv[:1] == [str(png)], f"argv={argv!r}")
    check("OCR is invoked as: <bin> <png> stdout -l eng --psm 6",
          argv[:5] == [str(png), "stdout", "-l", "eng", "--psm"],
          f"argv={argv!r}")

    fail = _stub(tmp, "tess_fail", "exit 3")
    expect_code("non-zero exit fails closed", l6.run_ocr, "ocr_failed",
                png, tesseract_bin=fail)

    big = _stub(tmp, "tess_big",
                'i=0; while [ $i -lt 400 ]; do printf '
                '"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\\n"; i=$((i+1)); done')
    text, truncated = l6.run_ocr(png, tesseract_bin=big, max_bytes=64)
    check("oversize output is bounded and flagged",
          truncated is True and len(text) <= 64,
          f"truncated={truncated} len={len(text)}")

    slow = _stub(tmp, "tess_slow", "sleep 10")
    started = time.time()
    expect_code("timeout fails closed", l6.run_ocr, "ocr_timeout",
                png, tesseract_bin=slow, timeout=0.5)
    check("timeout is honoured (child killed, bounded)",
          time.time() - started < 5.0, f"elapsed={time.time() - started}")

    expect_code("missing engine fails closed", l6.run_ocr, "ocr_unavailable",
                png, tesseract_bin=str(Path(tmp) / "does-not-exist"))


# ══ 3. OCR classification of ONE candidate ════════════════════════════

print("\nTest 3: analyze_ocr_text — the candidate acceptance test")

check("a well-formed Weekly Six is valid",
      l6.analyze_ocr_text(VALID_OCR) == ("valid", ""),
      f"{l6.analyze_ocr_text(VALID_OCR)!r}")
check("a marker-free post is simply absent (not a candidate)",
      l6.analyze_ocr_text(ORDINARY_OCR)[0] == "absent")
check("no text at all is absent",
      l6.analyze_ocr_text("   ")[0] == "absent")
check("a marker present twice is ambiguous",
      l6.analyze_ocr_text(AMBIGUOUS_OCR)[0] == "ambiguous",
      f"{l6.analyze_ocr_text(AMBIGUOUS_OCR)!r}")
check("two WEEK OF labels are ambiguous",
      l6.analyze_ocr_text(VALID_OCR + "\nWEEK OF 09.28.26")[0] == "ambiguous")
check("a marker with no WEEK OF label is malformed",
      l6.analyze_ocr_text(VALID_OCR.replace("WEEK OF 09.21.26", ""))[0]
      == "malformed")
check("a marker with only five rows is malformed",
      l6.analyze_ocr_text(MALFORMED_OCR)[0] == "malformed",
      f"{l6.analyze_ocr_text(MALFORMED_OCR)!r}")
check("a repeated weekday (so not six distinct) is malformed",
      l6.analyze_ocr_text(VALID_OCR.replace("TUESDAY 09.22", "MONDAY 09.21"))[0]
      == "malformed")
check("truncated OCR cannot be trusted",
      l6.analyze_ocr_text(VALID_OCR, truncated=True)[0] == "truncated")
check("the case of the marker does not matter",
      l6.analyze_ocr_text(VALID_OCR.replace("THE WEEKLY SIX", "the weekly six"))[0]
      == "valid")


print("\nTest 4: real published-image OCR corruption is canonicalized")

OBSERVED_BLOCK_OCR = """Level6 Training is at Level6 Training
THE WEEKLY¥S> § >< WEEK OF
i. MON >> FULL BODY S&C “=
: s TUE >» ABS & GLUTES
| 2 WED >> UPPER BODY DROPSETS
7 7 TH >> ATHLETIC CONDITIONING
i 0° FRI >> LOWER BODY TRIPLESETS ¢
: SAT » CARDIO MAYHEM ©
LEVEL6TRAINING.COM
"""
OBSERVED_SPARSE_OCR = """WEEK OF

THE WEEKLY¥=> ff 3

09.14.26
"""
EXPECTED_CANONICAL = "\n".join([
    "THE WEEKLY SIX",
    "WEEK OF 09.14.26",
    "Monday 09.14 FULL BODY S&C",
    "Tuesday 09.15 ABS & GLUTES",
    "Wednesday 09.16 UPPER BODY DROPSETS",
    "Thursday 09.17 ATHLETIC CONDITIONING",
    "Friday 09.18 LOWER BODY TRIPLESETS",
    "Saturday 09.19 CARDIO MAYHEM",
])

canonical = l6.canonicalize_weekly_ocr(
    OBSERVED_BLOCK_OCR, OBSERVED_SPARSE_OCR)
check("the actual stylised-heading OCR normalizes to the strict contract",
      canonical == EXPECTED_CANONICAL, f"{canonical!r}")
check("the canonical output passes the existing strict analyzer",
      l6.analyze_ocr_text(canonical) == ("valid", ""))
expect_code("a marker-free image remains an ordinary post",
            l6.canonicalize_weekly_ocr, "marker_absent",
            "ordinary gym post", "ordinary gym post")
expect_code("five arrow rows fail closed",
            l6.canonicalize_weekly_ocr, "malformed_newest",
            "\n".join(OBSERVED_BLOCK_OCR.splitlines()[:-2]),
            OBSERVED_SPARSE_OCR)
expect_code("two different printed weeks are ambiguous",
            l6.canonicalize_weekly_ocr, "ambiguous",
            OBSERVED_BLOCK_OCR + " WEEK OF 09.21.26",
            OBSERVED_SPARSE_OCR)
expect_code("a non-Monday printed week fails closed",
            l6.canonicalize_weekly_ocr, "malformed_newest",
            OBSERVED_BLOCK_OCR,
            OBSERVED_SPARSE_OCR.replace("09.14.26", "09.15.26"))


with tempfile.TemporaryDirectory(prefix="l6-layout-") as tmp:
    source = Path(tmp) / "candidate.png"
    source.write_bytes(b"fake")
    convert = _stub(tmp, "convert_ok", 'cp "$1" "$8"')
    tesseract = _stub(
        tmp, "tess_layout",
        'case "$*" in *"--psm 6"*) printf "%s" "$BLOCK" ;; '
        '*) printf "%s" "$SPARSE" ;; esac')
    env_before = dict(os.environ)
    os.environ["BLOCK"] = OBSERVED_BLOCK_OCR
    os.environ["SPARSE"] = OBSERVED_SPARSE_OCR
    try:
        text, truncated = l6.run_weekly_ocr(
            source, convert_bin=convert, tesseract_bin=tesseract)
    finally:
        os.environ.clear()
        os.environ.update(env_before)
    check("the two-pass runner returns canonical text",
          text == EXPECTED_CANONICAL and truncated is False, f"{text!r}")
    check("the preprocessed image is always deleted",
          not source.with_suffix(".ocr.png").exists())


# ══ 4. content-blind, newest-first candidate listing ══════════════════

print("\nTest 4: bounded, content-blind, newest-first candidate listing")


class _Href:
    def __init__(self, href):
        self._href = href

    def get_attribute(self, name, timeout=None):
        return self._href


class _NoHref:
    def get_attribute(self, name, timeout=None):
        raise RuntimeError("no href")


class _Link:
    def __init__(self, href):
        self.first = _Href(href) if href else _NoHref()


class _El:
    """A visible post element with an OPTIONAL permalink — never any text."""

    def __init__(self, visible=True, permalink=""):
        self._visible = visible
        self._permalink = permalink

    def is_visible(self):
        return self._visible

    def locator(self, selector):  # noqa: ARG002
        return _Link(self._permalink)


class _Articles:
    def __init__(self, page):
        self._page = page

    def count(self):
        return len(self._page.elements())

    def nth(self, index):
        return self._page.elements()[index]


class _Mouse:
    def __init__(self, page):
        self._page = page
        self.calls = []

    def wheel(self, dx, dy):
        self.calls.append((dx, dy))
        self._page.advance()


class _Page:
    """A scripted lazy-loading feed: one element list per scan."""

    def __init__(self, script):
        self._script = script
        self._index = 0
        self.mouse = _Mouse(self)
        self.waits = 0

    def elements(self):
        return self._script[min(self._index, len(self._script) - 1)]

    def advance(self):
        self._index = min(self._index + 1, len(self._script) - 1)

    def locator(self, selector):  # noqa: ARG002
        return _Articles(self)

    def wait_for_timeout(self, ms):  # noqa: ARG002
        self.waits += 1


page = _Page([[_El(permalink="p1"), _El(permalink="p2"), _El(permalink="p3")]])
listed = bo._list_level6_candidates(page, max_candidates=6, max_scrolls=4,
                                    scan_pause_ms=0)
check("candidates are listed in DOM (newest-first) order",
      listed == [{"index": 0, "permalink": "p1"},
                 {"index": 1, "permalink": "p2"},
                 {"index": 2, "permalink": "p3"}], f"{listed!r}")
check("listing is CONTENT-BLIND — no DOM text is read for the marker",
      all(isinstance(c, dict) and "text" not in c for c in listed), f"{listed!r}")

invisible = _Page([[_El(visible=False), _El(permalink="p2")]])
ls2 = bo._list_level6_candidates(invisible, max_candidates=6, max_scrolls=0,
                                 scan_pause_ms=0)
check("invisible articles are skipped",
      ls2 == [{"index": 1, "permalink": "p2"}], f"{ls2!r}")

many = _Page([[_El(permalink=f"p{i}") for i in range(5)]])
ls3 = bo._list_level6_candidates(many, max_candidates=2, max_scrolls=0,
                                 scan_pause_ms=0)
check("listing is bounded to max_candidates",
      len(ls3) == 2 and ls3[0]["index"] == 0 and ls3[1]["index"] == 1,
      f"{ls3!r}")

late = _Page([[_El(visible=False)], [_El(visible=False)],
              [_El(permalink="p9")]])
ls4 = bo._list_level6_candidates(late, max_candidates=6, max_scrolls=5,
                                 scan_pause_ms=0)
check("a lazy-loaded post is found after internal scrolling",
      ls4 == [{"index": 0, "permalink": "p9"}], f"{ls4!r}")
check("scrolls stayed within the bound", len(late.mouse.calls) <= 5,
      f"calls={late.mouse.calls!r}")

empty = _Page([[_El(visible=False)]])
check("no visible articles -> no candidates",
      bo._list_level6_candidates(empty, max_candidates=6, max_scrolls=3,
                                 scan_pause_ms=0) == [])
check("scrolling is exactly bounded by max_scrolls",
      len(empty.mouse.calls) == 3, f"calls={empty.mouse.calls!r}")


class _FlipEl:
    """Visible on the listing pass, gone on the re-read (a re-render)."""

    def __init__(self):
        self._n = 0

    def is_visible(self):
        self._n += 1
        return self._n == 1

    def locator(self, selector):  # noqa: ARG002
        return _Link("")


flip = _Page([[_FlipEl()]])
try:
    bo._list_level6_candidates(flip, max_candidates=6, max_scrolls=0)
    check("a mid-read re-render is refused", False, "no DriverError raised")
except bo.DriverError as exc:
    check("a mid-read re-render is refused (ordering_untrusted)",
          exc.code == "ordering_untrusted", f"code={exc.code!r}")


# ══ 5. the real capture seam: primary IMAGE, article fallback ═════════

print("\nTest 5: PlaywrightDriver.capture_level6_candidate picks the image")


class _Img:
    def __init__(self, area, visible=True, src="https://img.example/a.png"):
        self._area = area
        self._visible = visible
        self._src = src
        self.shots = []

    def is_visible(self):
        return self._visible

    def bounding_box(self):
        if self._area is None:
            return None
        return {"width": self._area, "height": 1}

    def get_attribute(self, name):
        return self._src if name == "src" else None

    def screenshot(self, path=None, timeout=None):  # noqa: ARG002
        self.shots.append(path)
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nimage")


class _ImgList:
    def __init__(self, imgs):
        self._imgs = imgs

    def count(self):
        return len(self._imgs)

    def nth(self, index):
        return self._imgs[index]


class _ArticleFull:
    def __init__(self, imgs):
        self._imgs = imgs
        self.article_shots = []

    def locator(self, selector):  # noqa: ARG002
        return _ImgList(self._imgs)

    def screenshot(self, path=None, timeout=None):  # noqa: ARG002
        self.article_shots.append(path)
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\narticle")


class _Nth:
    def __init__(self, items):
        self._items = items

    def nth(self, index):
        return self._items[index]


class _PageFull:
    def __init__(self, article):
        self._article = article

    def locator(self, selector):  # noqa: ARG002
        return _Nth([self._article])


with tempfile.TemporaryDirectory(prefix="l6-cap-") as tmp:
    avatar, graphic = _Img(40), _Img(900)
    article = _ArticleFull([avatar, graphic])
    driver = bo.PlaywrightDriver(Path(tmp))
    driver._page = _PageFull(article)
    shot = str(Path(tmp) / "a.png")
    driver.capture_level6_candidate({"index": 0}, shot)
    check("the LARGEST visible image is captured (the workout graphic)",
          graphic.shots == [shot] and avatar.shots == [] and
          article.article_shots == [], f"{graphic.shots!r} {avatar.shots!r}")

    no_images = _ArticleFull([])
    driver._page = _PageFull(no_images)
    shot2 = str(Path(tmp) / "b.png")
    driver.capture_level6_candidate({"index": 0}, shot2)
    check("with no usable image the whole article is captured",
          no_images.article_shots == [shot2], f"{no_images.article_shots!r}")

    check("only visible images are considered",
          bo._largest_visible_image(_ArticleFull([_Img(900, visible=False)]))
          is None)
    big, small = _Img(60), _Img(20)
    check("_largest_visible_image returns the biggest visible box",
          bo._largest_visible_image(_ArticleFull([small, big])) is big)
    check("_largest_visible_image tolerates boxless images",
          bo._largest_visible_image(_ArticleFull([_Img(None), small]))
          is small)


class _PhotoPage:
    def __init__(self, images):
        self._images = images

    def locator(self, selector):
        assert selector == "img"
        return _ImgList(self._images)


class _PhotoImg(_Img):
    def bounding_box(self):
        if self._area is None:
            return None
        return {"width": self._area, "height": self._area}


small, hidden, first, second = (_PhotoImg(80), _PhotoImg(900, False),
                                _PhotoImg(400, src="https://img.example/first"),
                                _PhotoImg(500, src="https://img.example/second"))
photos = bo._list_level6_photos(_PhotoPage([small, hidden, first, second]),
                                max_candidates=6)
check("Photos listing ignores small/hidden images and preserves DOM order",
      [p["index"] for p in photos] == [2, 3], f"{photos!r}")
check("Photos listing is bounded", len(bo._list_level6_photos(
      _PhotoPage([first, second]), max_candidates=1)) == 1)
rotating_a = _PhotoImg(400, src="https://scontent.example/photo.jpg?token=one")
rotating_b = _PhotoImg(400, src="https://other-cdn.example/photo.jpg?token=two")
different = _PhotoImg(400, src="https://scontent.example/other.jpg?token=one")
check("rotating Facebook CDN query/host values do not change identity",
      bo._level6_photo_key(rotating_a) == bo._level6_photo_key(rotating_b))
check("different Facebook image paths retain different identities",
      bo._level6_photo_key(rotating_a) != bo._level6_photo_key(different))

with tempfile.TemporaryDirectory(prefix="l6-photo-cap-") as tmp:
    driver = bo.PlaywrightDriver(Path(tmp))
    # Facebook may rotate every part of the signed image URL between listing
    # and capture. The bounded grid slot, not URL identity, is the handle.
    moved = _PhotoImg(400, src="https://img.example/completely-new?token=2")
    chrome = _PhotoImg(300, src="https://img.example/chrome")
    driver._page = _PhotoPage([chrome, moved])
    shot = str(Path(tmp) / "moved.png")
    driver.capture_level6_photo(
        {"index": 1, "key": "an-intentionally-stale-fingerprint"}, shot)
    check("Photos capture uses the bounded slot despite URL rotation",
          moved.shots == [shot] and chrome.shots == [],
          f"moved={moved.shots!r} chrome={chrome.shots!r}")

    unavailable = _PhotoImg(None, src="https://img.example/unavailable")
    driver._page = _PhotoPage([unavailable])
    try:
        driver.capture_level6_photo({"index": 0}, shot)
        check("an unavailable grid slot fails closed", False,
              "no DriverError raised")
    except bo.DriverError as exc:
        check("an unavailable grid slot fails closed",
              exc.code == "ordering_untrusted", f"code={exc.code!r}")


# ══ 6. the operation: newest selection + response contract ════════════

print("\nTest 6: the operation selects the NEWEST well-formed Weekly Six")


class FakeDriver:
    def __init__(self, candidates=None, final_url=PAGE, scan_error=None,
                 capture="write", capture_raises=False):
        self._candidates = list(candidates if candidates is not None
                                else [CAND0])
        self._final_url = final_url
        self._scan_error = scan_error
        self._capture = capture
        self._capture_raises = capture_raises
        self.navigated = []
        self.scan_calls = []
        self.captured = []
        self.screenshots = []

    def open(self):
        return None

    def close(self):
        return None

    def navigate(self, url):
        self.navigated.append(url)

    def current_url(self):
        return self._final_url

    def scan_level6_candidates(self, *, max_candidates=6, max_scrolls=8,
                               **kwargs):  # noqa: ARG002
        self.scan_calls.append({"max_candidates": max_candidates,
                                "max_scrolls": max_scrolls})
        if self._scan_error is not None:
            raise self._scan_error
        return [dict(c) for c in self._candidates]

    def scan_level6_photos(self, *, max_candidates=6):
        self.scan_calls.append({"max_candidates": max_candidates})
        if self._scan_error is not None:
            raise self._scan_error
        return [dict(c) for c in self._candidates]

    def capture_level6_candidate(self, descriptor, path):
        self.captured.append(dict(descriptor))
        self.screenshots.append(path)
        if self._capture_raises:
            if isinstance(self._capture_raises, Exception):
                raise self._capture_raises
            raise RuntimeError("element capture blew up")
        if self._capture == "write":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfake")

    def capture_level6_photo(self, descriptor, path):
        self.capture_level6_candidate(descriptor, path)


def ocr_sequence(items):
    """An ocr_runner that yields pre-scripted results, one per capture."""
    stream = list(items)

    def runner(path):  # noqa: ARG001
        if not stream:
            return ("", False)
        item = stream.pop(0)
        if isinstance(item, tuple):
            return item
        if isinstance(item, Exception):
            raise item
        return (item, False)

    return runner


def run_op(driver=None, proto=None, ocr_runner=None,
           allowlist=("facebook.com",), root=None, **kwargs):
    driver = FakeDriver() if driver is None else driver
    proto = bo.FakeProto() if proto is None else proto
    ocr_runner = ocr_runner or ocr_sequence([VALID_OCR])
    return l6.run_level6_weekly(driver, proto, root=root,
                                allowlist=list(allowlist),
                                ocr_runner=ocr_runner, **kwargs)


def all_cleaned(driver):
    return bool(driver.screenshots) and all(
        not os.path.exists(p) for p in driver.screenshots)


with tempfile.TemporaryDirectory(prefix="l6-op-") as root:
    driver = FakeDriver(candidates=[CAND0, CAND1, CAND2])
    proto = bo.FakeProto()
    res = run_op(driver=driver, proto=proto, root=root)
    payload = res["level6_weekly"]
    check("status is a clean no_changes", res["status"] == "no_changes",
          f"{res!r}")
    check("the NEWEST candidate (index 0) was selected",
          payload["scan_index"] == 0 and payload["post_ref"], f"{payload!r}")
    check("only the selected candidate was captured (no wasted work)",
          [c["index"] for c in driver.captured] == [0], f"{driver.captured!r}")
    check("candidates_scanned is 1", payload["candidates_scanned"] == 1)
    check("browser_artifacts is empty (no image metadata crosses)",
          res["browser_artifacts"] == [], f"{res['browser_artifacts']!r}")
    check("final_response carries NO page text", res["final_response"] == "",
          f"{res['final_response']!r}")
    check("no .png reference anywhere in the response",
          ".png" not in json.dumps(res), f"{json.dumps(res)[:200]!r}")
    check("structured OCR text is returned",
          payload["ocr_text"] == VALID_OCR, f"{payload['ocr_text']!r}")
    check("ocr_engine is recorded", payload["ocr_engine"] == "tesseract")
    check("ocr_truncated is False", payload["ocr_truncated"] is False)
    check("marker metadata is present", payload["marker"] == MARKER)
    check("pinned page recorded", payload["page_url"] == PAGE)
    check("Photos page metadata preserved", payload["permalink"] == PAGE)
    check("only the granted read-only ops are announced",
          proto.operations == ["browser.navigate", "browser.read",
                               "browser.screenshot"],
          f"{proto.operations!r}")
    check("navigation used the PINNED url", driver.navigated == [PAGE],
          f"{driver.navigated!r}")
    check("the candidate cap was passed to the Photos driver",
          driver.scan_calls == [{"max_candidates": l6.MAX_CANDIDATES}],
          f"{driver.scan_calls!r}")
    check("the PNG was cleaned up after OCR", all_cleaned(driver),
          f"shots={driver.screenshots!r}")


print("\nTest 7: the marker is NOT in the caption/DOM — only in the image OCR")


with tempfile.TemporaryDirectory(prefix="l6-op-") as root:
    # The driver is asked for candidates with NO marker argument at all: the
    # operation must never rely on DOM text to find the post.
    driver = FakeDriver(candidates=[CAND0])
    res = run_op(driver=driver, ocr_runner=ocr_sequence([VALID_OCR]), root=root)
    check("discovery succeeds with the marker present ONLY in the OCR",
          res["status"] == "no_changes", f"{res!r}")
    check("the driver was never handed the marker for DOM matching",
          set(driver.scan_calls[0]) == {"max_candidates"},
          f"{driver.scan_calls!r}")


print("\nTest 8: newer non-Weekly-Six posts are skipped (bounded) then found")


with tempfile.TemporaryDirectory(prefix="l6-op-") as root:
    driver = FakeDriver(candidates=[CAND0, CAND1])
    res = run_op(driver=driver,
                 ocr_runner=ocr_sequence([ORDINARY_OCR, VALID_OCR]), root=root)
    payload = res["level6_weekly"]
    check("an ordinary newer post is skipped for the weekly-six below it",
          payload["scan_index"] == 1 and payload["candidates_scanned"] == 2,
          f"{payload!r}")
    check("both candidates were captured, in order",
          [c["index"] for c in driver.captured] == [0, 1], f"{driver.captured!r}")
    check("both PNGs were cleaned up", all_cleaned(driver),
          f"shots={driver.screenshots!r}")


print("\nTest 9: bounded scanning — the cap is enforced")


with tempfile.TemporaryDirectory(prefix="l6-op-") as root:
    ten = [{"index": i,
            "permalink": f"https://www.facebook.com/level6training/posts/{i}"}
           for i in range(10)]
    driver = FakeDriver(candidates=ten)
    # The only valid post sits past the default cap -> never reached.
    res = run_op(driver=driver, root=root,
                 ocr_runner=ocr_sequence([ORDINARY_OCR] * 7 + [VALID_OCR]))
    check("a valid post beyond the cap is not found (bounded scan)",
          res["level6_weekly"]["error_code"] == "no_candidate",
          f"{res['level6_weekly']!r}")
    check("capture is bounded to exactly MAX_CANDIDATES",
          len(driver.captured) == l6.MAX_CANDIDATES, f"{len(driver.captured)}")
    check("every bounded PNG was cleaned up", all_cleaned(driver))

    driver = FakeDriver(candidates=ten)
    res = run_op(driver=driver, root=root, max_candidates=3,
                 ocr_runner=ocr_sequence([ORDINARY_OCR] * 3 + [VALID_OCR]))
    check("a caller-side (test) cap of 3 is honoured",
          len(driver.captured) == 3, f"{driver.captured!r}")


print("\nTest 10: a malformed NEWER post is refused — never an older valid one")


with tempfile.TemporaryDirectory(prefix="l6-op-") as root:
    driver = FakeDriver(candidates=[CAND0, CAND1])
    res = run_op(driver=driver, root=root,
                 ocr_runner=ocr_sequence([MALFORMED_OCR, VALID_OCR]))
    check("a malformed newest Weekly Six fails closed",
          res["level6_weekly"]["error_code"] == "malformed_newest",
          f"{res['level6_weekly']!r}")
    check("the message says the newest post is not well formed",
          "not well formed" in res["errors"][0], f"{res['errors']!r}")
    check("the OLDER valid post is never even captured",
          [c["index"] for c in driver.captured] == [0], f"{driver.captured!r}")
    check("the malformed candidate PNG was cleaned up", all_cleaned(driver))


print("\nTest 11: an AMBIGUOUS newest post is refused")


with tempfile.TemporaryDirectory(prefix="l6-op-") as root:
    driver = FakeDriver(candidates=[CAND0, CAND1])
    res = run_op(driver=driver, root=root,
                 ocr_runner=ocr_sequence([AMBIGUOUS_OCR, VALID_OCR]))
    check("an ambiguous newest candidate fails closed",
          res["level6_weekly"]["error_code"] == "ambiguous",
          f"{res['level6_weekly']!r}")
    check("the message says the newest candidates are ambiguous",
          "ambiguous" in res["errors"][0], f"{res['errors']!r}")
    check("the older valid post is never captured",
          [c["index"] for c in driver.captured] == [0], f"{driver.captured!r}")
    check("the ambiguous candidate PNG was cleaned up", all_cleaned(driver))


# ══ 7. failure codes, denials, cleanup ════════════════════════════════

print("\nTest 12: no candidate / ordering / page identity")


with tempfile.TemporaryDirectory(prefix="l6-op-") as root:
    driver = FakeDriver(candidates=[])
    res = run_op(driver=driver, root=root)
    check("no visible candidates -> no_candidate",
          res["level6_weekly"]["error_code"] == "no_candidate",
          f"{res['level6_weekly']!r}")
    check("the no-candidate message names the marker",
          MARKER in res["errors"][0], f"{res['errors']!r}")
    check("no candidate -> nothing captured", driver.captured == [])

    # Visible posts, but none is a Weekly Six -> no_candidate after the scan.
    driver = FakeDriver(candidates=[CAND0, CAND1])
    res = run_op(driver=driver, root=root,
                 ocr_runner=ocr_sequence([ORDINARY_OCR, ORDINARY_OCR]))
    check("no credible candidate -> no_candidate",
          res["level6_weekly"]["error_code"] == "no_candidate")
    check("the non-credible PNGs were all cleaned up", all_cleaned(driver))

    driver = FakeDriver(scan_error=bo.DriverError("feed moved",
                                                  code="ordering_untrusted"))
    res = run_op(driver=driver, root=root)
    check("an untrustworthy feed order fails closed",
          res["level6_weekly"]["error_code"] == "ordering_untrusted",
          f"{res['level6_weekly']!r}")

    class ForeignDriverError(Exception):
        """Models browser_operator executed as __main__ in production."""
        code = "ordering_untrusted"

    driver = FakeDriver(scan_error=ForeignDriverError("must not leak"))
    res = run_op(driver=driver, root=root)
    check("foreign DriverError class preserves its structured code",
          res["level6_weekly"]["error_code"] == "ordering_untrusted",
          f"{res['level6_weekly']!r}")
    check("foreign DriverError message is not exposed",
          "must not leak" not in json.dumps(res), json.dumps(res)[:300])

    driver = FakeDriver(scan_error=RuntimeError("boom"))
    res = run_op(driver=driver, root=root)
    check("a generic scan error -> locate_failed",
          res["level6_weekly"]["error_code"] == "locate_failed",
          f"{res['level6_weekly']!r}")
    check("generic scan diagnostic exposes only phase + exception class",
          res["errors"] == ["photos_list:RuntimeError"],
          f"{res['errors']!r}")
    leaked = json.dumps(res)
    check("generic scan diagnostic leaks no exception detail or artifact",
          all(token not in leaked for token in (
              "boom", "scontent", "/tmp/", ".png", "<html", "OCR text")),
          leaked[:300])

    driver = FakeDriver(final_url="https://www.facebook.com/login/?next=/x")
    res = run_op(driver=driver, root=root)
    check("login wall -> page_identity",
          res["level6_weekly"]["error_code"] == "page_identity")

    proto = bo.FakeProto()
    driver = FakeDriver(final_url="https://evil.example/x")
    res = run_op(driver=driver, proto=proto, root=root)
    check("foreign origin -> page_identity",
          res["level6_weekly"]["error_code"] == "page_identity")
    check("no read/capture after a failed identity check",
          proto.operations == ["browser.navigate"], f"{proto.operations!r}")


print("\nTest 13: per-operation denial stops before any later op")


with tempfile.TemporaryDirectory(prefix="l6-op-") as root:
    driver, proto = FakeDriver(), bo.FakeProto(allow=set())
    res = run_op(driver=driver, proto=proto, root=root)
    check("navigate denial -> navigate_denied",
          res["level6_weekly"]["error_code"] == "navigate_denied")
    check("navigate denial stops immediately",
          proto.operations == ["browser.navigate"], f"{proto.operations!r}")

    driver = FakeDriver()
    proto = bo.FakeProto(allow={"browser.navigate"})
    res = run_op(driver=driver, proto=proto, root=root)
    check("read denial -> read_denied",
          res["level6_weekly"]["error_code"] == "read_denied")
    check("read denial announces only navigate+read",
          proto.operations == ["browser.navigate", "browser.read"],
          f"{proto.operations!r}")

    driver = FakeDriver()
    proto = bo.FakeProto(allow={"browser.navigate", "browser.read"})
    res = run_op(driver=driver, proto=proto, root=root)
    check("screenshot denial -> screenshot_denied",
          res["level6_weekly"]["error_code"] == "screenshot_denied")
    check("screenshot denial never captures",
          driver.screenshots == [], f"{driver.screenshots!r}")

    res = run_op(allowlist=(), root=root)
    check("empty allowlist -> not_allowlisted",
          res["level6_weekly"]["error_code"] == "not_allowlisted")


print("\nTest 14: capture + OCR failure paths, all cleaned up")


with tempfile.TemporaryDirectory(prefix="l6-op-") as root:
    driver = FakeDriver(capture_raises=True)
    res = run_op(driver=driver, root=root)
    check("capture failure -> capture_failed",
          res["level6_weekly"]["error_code"] == "capture_failed")
    check("a failed capture leaves no PNG behind", all_cleaned(driver))

    class ForeignCaptureError(Exception):
        code = "ordering_untrusted"

    driver = FakeDriver(capture_raises=ForeignCaptureError("must not leak"))
    res = run_op(driver=driver, root=root)
    check("foreign capture error preserves its structured code",
          res["level6_weekly"]["error_code"] == "ordering_untrusted",
          f"{res['level6_weekly']!r}")
    check("foreign capture error message is not exposed",
          "must not leak" not in json.dumps(res), json.dumps(res)[:300])

    driver = FakeDriver(capture="none")
    res = run_op(driver=driver, root=root)
    check("capture produced no file -> capture_missing",
          res["level6_weekly"]["error_code"] == "capture_missing")

    def boom(path):  # noqa: ARG001
        raise l6.Level6OcrError("ocr_timeout", "the OCR engine exceeded its budget")

    driver = FakeDriver()
    res = run_op(driver=driver, ocr_runner=boom, root=root)
    check("OCR timeout -> ocr_timeout",
          res["level6_weekly"]["error_code"] == "ocr_timeout")
    check("the PNG was cleaned up after an OCR failure", all_cleaned(driver))

    driver = FakeDriver()
    res = run_op(driver=driver,
                 ocr_runner=ocr_sequence([l6.Level6OcrError("ocr_unavailable",
                                                           "no engine")]),
                 root=root)
    check("OCR unavailable -> ocr_unavailable",
          res["level6_weekly"]["error_code"] == "ocr_unavailable")
    check("the PNG was cleaned up after OCR unavailability", all_cleaned(driver))

    driver = FakeDriver()
    res = run_op(driver=driver, ocr_runner=ocr_sequence([RuntimeError("x")]),
                 root=root)
    check("a non-OCR exception in the runner -> ocr_failed",
          res["level6_weekly"]["error_code"] == "ocr_failed")
    check("the PNG was cleaned up after a generic OCR failure",
          all_cleaned(driver))

    driver = FakeDriver()
    res = run_op(driver=driver,
                 ocr_runner=ocr_sequence([(VALID_OCR, True)]), root=root)
    check("oversize/truncated OCR fails closed (ocr_truncated)",
          res["level6_weekly"]["error_code"] == "ocr_truncated",
          f"{res['level6_weekly']!r}")
    check("the truncated candidate PNG was cleaned up", all_cleaned(driver))


# ══ 8. operator integration: spec, preflight, run_actions routing ═════

print("\nTest 15: operator integration — spec, preflight, run_actions routing")

import host_allowlist  # noqa: E402 — the host's own defense-in-depth preflight

spec_text = json.dumps({"level6_weekly": True, "url": PAGE})
actions = bo.parse_spec(spec_text)
check("parse_spec yields the single fixed-purpose action",
      actions == [{"action": "level6_weekly", "url": PAGE}], f"{actions!r}")
check("parse_spec accepts the Level 6 photos URL (the pinned page)",
      bo.parse_spec(json.dumps({
          "level6_weekly": True,
          "url": "https://www.facebook.com/level6training/photos"}))
      == [{"action": "level6_weekly", "url": PAGE}])

for bad, why in (
    (json.dumps({"level6_weekly": False, "url": PAGE}), "flag not True"),
    (json.dumps({"level6_weekly": True,
                 "url": "https://www.facebook.com/level6training/"}),
     "the OLD timeline url is no longer accepted"),
    (json.dumps({"level6_weekly": True,
                 "url": "https://www.facebook.com/level6training/photos/"}),
     "photos url must match exactly (trailing slash rejected)"),
    (json.dumps({"level6_weekly": True,
                 "url": "https://www.facebook.com/level6training/photos?x=1"}),
     "photos url must match exactly (query rejected)"),
    (json.dumps({"level6_weekly": True,
                 "url": "https://evil.example/level6training/photos"}),
     "arbitrary origin"),
    (json.dumps({"level6_weekly": True, "url": "https://evil.example/x"}),
     "caller-supplied url"),
    (json.dumps({"level6_weekly": True, "url": PAGE, "date": "2026-09-21"}),
     "caller-supplied date"),
    (json.dumps({"level6_weekly": True, "url": PAGE, "max_candidates": 99}),
     "caller-supplied cap"),
    (json.dumps({"level6_weekly": True, "url": PAGE, "actions": []}),
     "extra actions key"),
):
    try:
        bo.parse_spec(bad)
        check(f"rejected spec ({why})", False, "no SpecError raised")
    except bo.SpecError:
        check(f"rejected spec ({why})", True)

check("cloud preflight allows the pinned page when allowlisted",
      bo.preflight(spec_text, ["facebook.com"]) == (True, ""))
allowed, reason = bo.preflight(spec_text, ["example.com"])
check("cloud preflight blocks the pinned page when not allowlisted",
      allowed is False and "allowlist" in reason, f"{reason!r}")
check("host preflight allows the pinned page when allowlisted",
      host_allowlist.preflight(spec_text, ["facebook.com"]) == (True, ""))
allowed, reason = host_allowlist.preflight(spec_text, ["example.com"])
check("host preflight blocks the pinned page when not allowlisted",
      allowed is False, f"{reason!r}")

real_weekly_ocr = l6.run_weekly_ocr
with tempfile.TemporaryDirectory(prefix="l6-run-") as root:
    l6.run_weekly_ocr = lambda path, **kw: (VALID_OCR, False)
    try:
        driver = FakeDriver(candidates=[CAND0])
        proto = bo.FakeProto()
        routed = bo.run_actions(
            [{"action": "level6_weekly", "url": PAGE}], driver,
            root=root, allowlist=["facebook.com"], proto=proto)
    finally:
        l6.run_weekly_ocr = real_weekly_ocr
check("run_actions routes the level6 action to the fixed operation",
      isinstance(routed.get("level6_weekly"), dict)
      and routed["level6_weekly"]["ocr_text"] == VALID_OCR, f"{routed!r}")
check("run_actions keeps image paths off the result",
      routed["browser_artifacts"] == [] and ".png" not in json.dumps(routed),
      f"{routed!r}")
check("run_actions cleaned the candidate PNG",
      all_cleaned(driver), f"shots={driver.screenshots!r}")


# ══ 9. the safe fixed Photos-element capture path (replacement regressions) ══

print("\nTest 16: fixed Photos-element capture — no download, no second browser")

_SAFE_SOURCE = Path(l6.__file__).read_text(encoding="utf-8")
_FORBIDDEN = ("urllib.request", "urlretrieve", "urlopen", "socket.socket",
              "sync_playwright", "PlaywrightDriver(", "requests.get",
              "requests.post")
check("(no raw URL download) the operation source has NO network primitive",
      not any(tok in _SAFE_SOURCE for tok in _FORBIDDEN),
      f"found={[t for t in _FORBIDDEN if t in _SAFE_SOURCE]!r}")


class BareDriver:
    """An UNauthorised driver: navigate only, NO fixed candidate API.

    The operation must fail closed against this — never reach for a transport
    of its own (a raw-URL fetch, a fresh browser session, an OCR-only shortcut).
    """

    def __init__(self, with_scan=True, with_capture=True):
        self._with_scan = with_scan
        self._with_capture = with_capture
        self.navigated = []

    def navigate(self, url):
        self.navigated.append(url)

    def current_url(self):
        return PAGE

    def scan_level6_photos(self, **kwargs):  # noqa: ARG002
        if not self._with_scan:
            raise AttributeError("scan_level6_photos")
        return [dict(CAND0)]

    def capture_level6_photo(self, descriptor, path):  # noqa: ARG002
        if not self._with_capture:
            raise AttributeError("capture_level6_photo")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nx")


with tempfile.TemporaryDirectory(prefix="l6-safe-") as root:
    # (no independent browser) With a scripted OCR runner the operation must
    # spawn NOTHING: no second Playwright/Chromium, no subprocess at all.
    real_popen = l6.subprocess.Popen
    spawned = []

    def _spy_popen(*args, **kwargs):
        spawned.append(args)
        return real_popen(*args, **kwargs)

    l6.subprocess.Popen = _spy_popen
    try:
        driver = FakeDriver()
        res = run_op(driver=driver, root=root,
                     ocr_runner=ocr_sequence([VALID_OCR]))
    finally:
        l6.subprocess.Popen = real_popen
    check("(no independent browser) nothing is spawned off the OCR path",
          spawned == [] and res["status"] == "no_changes",
          f"spawned={spawned!r} {res!r}")

    # (existing authorized driver required) A driver missing the fixed
    # candidate API fails closed — the operation never substitutes its own.
    res = run_op(driver=BareDriver(with_scan=False), root=root)
    check("(driver required) missing candidate API -> locate_failed",
          res["level6_weekly"]["error_code"] == "locate_failed",
          f"{res['level6_weekly']!r}")
    res = run_op(driver=BareDriver(with_capture=False), root=root)
    check("(driver required) missing element capture -> capture_failed",
          res["level6_weekly"]["error_code"] == "capture_failed",
          f"{res['level6_weekly']!r}")

    # (no raw URL download) A raw image URL in a descriptor is never forwarded
    # to or fetched by the operation: only the fixed index/permalink descriptor
    # crosses into the driver, and the raw URL never reaches the result.
    cand_with_url = {"index": 0, "permalink": CAND0["permalink"],
                     "image_url": "https://scontent.example/raw.jpg"}
    driver = FakeDriver(candidates=[cand_with_url])
    res = run_op(driver=driver, root=root)
    check("(no raw URL download) a descriptor's raw image URL is dropped",
          len(driver.captured) == 1 and "image_url" not in driver.captured[0],
          f"{driver.captured!r}")
    check("(no raw URL download) the raw URL never reaches the result",
          "raw.jpg" not in json.dumps(res), f"{json.dumps(res)[:200]!r}")

    # (no generic text-only result) The marker alone is NOT a valid post: it is
    # refused, and a success always carries the REAL OCR text + no page text.
    driver = FakeDriver()
    res = run_op(driver=driver, root=root,
                 ocr_runner=ocr_sequence(["Level 6 Training\n" + MARKER]))
    check("(no generic result) marker-only OCR is refused (malformed_newest)",
          res["level6_weekly"]["error_code"] == "malformed_newest",
          f"{res['level6_weekly']!r}")
    check("(no generic result) a failure fabricates no OCR/marker payload",
          res["final_response"] == ""
          and "ocr_text" not in res["level6_weekly"],
          f"{res!r}")

    driver = FakeDriver()
    res = run_op(driver=driver, root=root)
    check("(no generic result) a success carries the REAL OCR text",
          res["level6_weekly"]["ocr_text"] == VALID_OCR
          and res["level6_weekly"]["ocr_text"] != MARKER,
          f"{res['level6_weekly']!r}")
    check("(no generic result) a success returns no page text / artifact",
          res["final_response"] == "" and res["browser_artifacts"] == [],
          f"{res!r}")


# ── Summary ───────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
