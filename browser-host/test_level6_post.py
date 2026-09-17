#!/usr/bin/env python3
"""Focused tests for the Browser Host Level 6 weekly operation.

Covers, against the REAL production code (no browser, no network):

  1. final-page identity validation — Facebook origin AND Level 6 page, with
     login/consent/other-origin/non-https rejected;
  2. the local OCR SUBPROCESS boundary — a real ``subprocess`` invocation
     against stub executables: success, non-zero exit, oversize output
     (truncated), hard timeout (child killed, bounded), and a missing engine;
  3. bounded post selection — internal scrolling (never a generic action) is
     capped at ``max_scrolls``, lazy-load retries find a late-rendered post,
     invisible elements are ignored, and multiple credible posts are returned
     for the caller to reject;
  4. cleanup — the host-local PNG is deleted on success AND on every failure;
  5. the response contract — structured OCR text + post metadata only; no PNG
     bytes, no PNG path, no ``browser_artifacts``, no ``final_response`` text;
  6. explicit failure codes — post_not_available, ambiguous_posts,
     page_identity, and per-operation denials (navigate/read/screenshot) that
     stop before any later operation is announced;
  7. the operation announces ONLY browser.navigate / browser.read /
     browser.screenshot.

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
POST_TEXT = "THE WEEKLY SIX\nWEEK OF 09.21.26\nMONDAY 09.21 Back Squat"


# ══ 1. final-page identity ════════════════════════════════════════════

print("\nTest 1: final Facebook origin + Level 6 page identity")

for url in (PAGE, "https://www.facebook.com/level6training",
            "https://www.facebook.com/level6training/posts/1234",
            "https://www.facebook.com/level6training/?ref=page_internal"):
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
    ("https://www.facebook.com/level6trainingfake/", "look-alike path prefix"),
    ("https://www.facebook.com/level6trainingXYZ/posts/1", "look-alike prefix"),
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
    # its first positional argument (with the FIXED language/psm flags after),
    # rather than the placeholder's implicit "trust me".
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


# ══ 3. bounded post selection ═════════════════════════════════════════

print("\nTest 3: bounded internal post selection (real scan code)")


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
    def __init__(self, text, visible=True, permalink=""):
        self._text = text
        self._visible = visible
        self._permalink = permalink

    def is_visible(self):
        return self._visible

    def inner_text(self, timeout=None):
        return self._text

    def locator(self, selector):
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
    """A scripted lazy-loading page: one element list per scan."""

    def __init__(self, script):
        self._script = script
        self._index = 0
        self.mouse = _Mouse(self)
        self.waits = 0

    def elements(self):
        return self._script[min(self._index, len(self._script) - 1)]

    def advance(self):
        self._index = min(self._index + 1, len(self._script) - 1)

    def locator(self, selector):
        return _Articles(self)

    def wait_for_timeout(self, ms):
        self.waits += 1


late = _Page([[_El("unrelated")], [_El("still unrelated")],
              [_El(POST_TEXT, permalink="https://www.facebook.com/level6training/posts/9")]])
found = bo._scan_level6_posts(late, MARKER, max_scrolls=5, scan_pause_ms=0)
check("lazy-loaded post is found after internal scrolling",
      len(found) == 1 and found[0]["text"] == POST_TEXT, f"found={found!r}")
check("scrolls stayed within the bound",
      len(late.mouse.calls) <= 5, f"calls={late.mouse.calls!r}")
check("permalink captured without following it",
      found[0]["permalink"].endswith("/posts/9"), f"{found[0]!r}")

never = _Page([[_El("nothing here")]])
check("no marker -> no candidates",
      bo._scan_level6_posts(never, MARKER, max_scrolls=3, scan_pause_ms=0) == [])
check("scrolling is exactly bounded by max_scrolls",
      len(never.mouse.calls) == 3, f"calls={never.mouse.calls!r}")

two = _Page([[_El(POST_TEXT, permalink="p1"), _El("noise"),
              _El(POST_TEXT, permalink="p2")]])
check("multiple credible posts are returned for the caller to reject",
      len(bo._scan_level6_posts(two, MARKER, max_scrolls=2, scan_pause_ms=0)) == 2)

hidden = _Page([[_El(POST_TEXT, visible=False)]])
check("an invisible marker post is not credible",
      bo._scan_level6_posts(hidden, MARKER, max_scrolls=0) == [])


# ══ 4-7. the operation ════════════════════════════════════════════════

print("\nTest 4: the operation's response contract")

GOOD_POST = {"index": 0, "text": POST_TEXT,
             "permalink": "https://www.facebook.com/level6training/posts/1"}


class FakeDriver:
    def __init__(self, posts=None, final_url=PAGE, capture=True,
                 capture_raises=False):
        self._posts = list(posts if posts is not None else [GOOD_POST])
        self._final_url = final_url
        self._capture = capture
        self._capture_raises = capture_raises
        self.navigated = []
        self.screenshots = []

    def open(self):
        return None

    def close(self):
        return None

    def navigate(self, url):
        self.navigated.append(url)

    def current_url(self):
        return self._final_url

    def query_level6_posts(self, marker, *, max_scrolls=8):
        return list(self._posts)

    def screenshot_element(self, descriptor, path):
        self.screenshots.append(path)
        if self._capture_raises:
            raise RuntimeError("element capture blew up")
        if self._capture:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfake")


def run_op(driver=None, proto=None, ocr_runner=None, allowlist=("facebook.com",),
           root=None):
    driver = FakeDriver() if driver is None else driver
    proto = bo.FakeProto() if proto is None else proto
    ocr_runner = ocr_runner or (lambda path: (POST_TEXT, False))
    return l6.run_level6_weekly(driver, proto, root=root,
                               allowlist=list(allowlist), ocr_runner=ocr_runner)


with tempfile.TemporaryDirectory(prefix="l6-op-") as root:
    driver = FakeDriver()
    proto = bo.FakeProto()
    res = run_op(driver=driver, proto=proto, root=root)
    payload = res["level6_weekly"]
    check("status is a clean no_changes", res["status"] == "no_changes",
          f"{res!r}")
    check("browser_artifacts is empty (no image metadata crosses)",
          res["browser_artifacts"] == [], f"{res['browser_artifacts']!r}")
    check("final_response carries NO page text", res["final_response"] == "",
          f"{res['final_response']!r}")
    check("no .png reference anywhere in the response",
          ".png" not in json.dumps(res), f"{json.dumps(res)[:200]!r}")
    check("structured OCR text is returned",
          payload["ocr_text"] == POST_TEXT, f"{payload['ocr_text']!r}")
    check("ocr_engine is recorded", payload["ocr_engine"] == "tesseract")
    check("ocr_truncated is False", payload["ocr_truncated"] is False)
    check("marker metadata is present", payload["marker"] == MARKER)
    check("pinned page recorded", payload["page_url"] == PAGE)
    check("post_ref is the stable content-derived id",
          payload["post_ref"] == l6._post_ref(GOOD_POST), f"{payload['post_ref']!r}")
    check("permalink metadata preserved",
          payload["permalink"].endswith("/posts/1"))
    check("only the three read-only ops are announced",
          proto.operations == ["browser.navigate", "browser.read",
                               "browser.screenshot"],
          f"{proto.operations!r}")
    check("navigation used the PINNED url",
          driver.navigated == [PAGE], f"{driver.navigated!r}")
    check("the PNG was cleaned up after OCR",
          len(driver.screenshots) == 1
          and not os.path.exists(driver.screenshots[0]),
          f"shots={driver.screenshots!r}")

print("\nTest 5: zero / multiple posts fail closed")

with tempfile.TemporaryDirectory(prefix="l6-op-") as root:
    res = run_op(driver=FakeDriver(posts=[]), root=root)
    check("no post -> post_not_available",
          res["level6_weekly"]["error_code"] == "post_not_available",
          f"{res['level6_weekly']!r}")
    check("no post -> the clear weekly-post-unavailable phrase",
          "weekly post not available" in res["errors"][0], f"{res['errors']!r}")

    res = run_op(driver=FakeDriver(posts=[GOOD_POST, dict(GOOD_POST, index=4)]),
                 root=root)
    check("two posts -> ambiguous_posts",
          res["level6_weekly"]["error_code"] == "ambiguous_posts",
          f"{res['level6_weekly']!r}")

print("\nTest 6: page identity and per-operation denial")

with tempfile.TemporaryDirectory(prefix="l6-op-") as root:
    res = run_op(driver=FakeDriver(
        final_url="https://www.facebook.com/login/?next=/level6training/"),
        root=root)
    check("login wall -> page_identity",
          res["level6_weekly"]["error_code"] == "page_identity",
          f"{res['level6_weekly']!r}")

    proto = bo.FakeProto()
    res = run_op(driver=FakeDriver(final_url="https://evil.example/x"),
                 proto=proto, root=root)
    check("foreign origin -> page_identity",
          res["level6_weekly"]["error_code"] == "page_identity")
    check("no read/screenshot after a failed identity check",
          proto.operations == ["browser.navigate"], f"{proto.operations!r}")

    proto = bo.FakeProto(allow=set())
    res = run_op(proto=proto, root=root)
    check("navigate denial -> navigate_denied",
          res["level6_weekly"]["error_code"] == "navigate_denied")
    check("navigate denial stops immediately",
          proto.operations == ["browser.navigate"], f"{proto.operations!r}")

    proto = bo.FakeProto(allow={"browser.navigate"})
    res = run_op(proto=proto, root=root)
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

print("\nTest 7: OCR failure paths + cleanup")

with tempfile.TemporaryDirectory(prefix="l6-op-") as root:
    def boom(path):
        raise l6.Level6OcrError("ocr_timeout", "the OCR engine exceeded its budget")

    driver = FakeDriver()
    res = run_op(driver=driver, ocr_runner=boom, root=root)
    check("OCR timeout -> ocr_timeout error_code",
          res["level6_weekly"]["error_code"] == "ocr_timeout")
    check("PNG cleaned up after an OCR failure",
          not os.path.exists(driver.screenshots[0]), f"{driver.screenshots!r}")

    driver = FakeDriver()
    res = run_op(driver=driver, ocr_runner=lambda p: ("   ", False), root=root)
    check("empty OCR -> ocr_empty",
          res["level6_weekly"]["error_code"] == "ocr_empty")
    check("PNG cleaned up after empty OCR",
          not os.path.exists(driver.screenshots[0]))

    driver = FakeDriver()
    res = run_op(driver=driver, ocr_runner=lambda p: (POST_TEXT, True), root=root)
    check("oversize OCR is flagged for the Cloud to reject",
          res["status"] == "no_changes"
          and res["level6_weekly"]["ocr_truncated"] is True,
          f"{res['level6_weekly']!r}")
    check("PNG cleaned up after a truncated OCR",
          not os.path.exists(driver.screenshots[0]))

    res = run_op(driver=FakeDriver(capture_raises=True), root=root)
    check("capture failure -> capture_failed",
          res["level6_weekly"]["error_code"] == "capture_failed")

    res = run_op(driver=FakeDriver(capture=False), root=root)
    check("capture produced no file -> capture_missing",
          res["level6_weekly"]["error_code"] == "capture_missing")


print("\nTest 8: operator integration — spec, preflight, run_actions routing")

import host_allowlist  # noqa: E402 — the host's own defense-in-depth preflight

spec_text = json.dumps({"level6_weekly": True, "url": PAGE})
actions = bo.parse_spec(spec_text)
check("parse_spec yields the single fixed-purpose action",
      actions == [{"action": "level6_weekly", "url": PAGE}], f"{actions!r}")

for bad, why in (
    (json.dumps({"level6_weekly": False, "url": PAGE}), "flag not True"),
    (json.dumps({"level6_weekly": True, "url": "https://evil.example/x"}),
     "caller-supplied url"),
    (json.dumps({"level6_weekly": True, "url": PAGE, "date": "2026-09-21"}),
     "caller-supplied date"),
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

real_ocr = l6.run_ocr
with tempfile.TemporaryDirectory(prefix="l6-run-") as root:
    l6.run_ocr = lambda path, **kw: (POST_TEXT, False)
    try:
        driver = FakeDriver()
        proto = bo.FakeProto()
        routed = bo.run_actions(
            [{"action": "level6_weekly", "url": PAGE}], driver,
            root=root, allowlist=["facebook.com"], proto=proto)
    finally:
        l6.run_ocr = real_ocr
check("run_actions routes the level6 action to the fixed operation",
      isinstance(routed.get("level6_weekly"), dict)
      and routed["level6_weekly"]["ocr_text"] == POST_TEXT, f"{routed!r}")
check("run_actions keeps image paths off the result",
      routed["browser_artifacts"] == [] and ".png" not in json.dumps(routed),
      f"{routed!r}")


# ── Summary ───────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
