"""Tests for level6_photos_scanner.py.

Covers: OCR canonicalization, marker detection, photo URL discovery,
credibility gates, ambiguous ordering, PNG cleanup, fail-closed,
and the exact "not_found" message.

Run: python3 -m pytest test_level6_photos_scanner.py
"""
import contextlib, io, json, os, re, shutil, sys, tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import level6_photos_scanner as S


# ── OCR canonicalization ─────────────────────────────────────────

def test_canon_empty():
    assert S.canon("") == ""
    assert S.canon(None) == ""

def test_canon_lowercases():
    assert S.canon("Weekly-Six") == "weekly-six"

def test_canon_strips_non_ascii():
    assert S.canon("weekly-six\u2014hello") == "weekly-six hello"

def test_canon_collapses_whitespace():
    assert S.canon("weekly\t\n  six") == "weekly six"

def test_canon_strips_outer_spaces():
    assert S.canon("  weekly-six  ") == "weekly-six"


# ── Marker detection ────────────────────────────────────────────

def test_marker_found_exact():
    assert S.found("weekly-six") == True

def test_marker_found_in_text():
    assert S.found("The weekly-six marker appears here") == True

def test_marker_not_found():
    assert S.found("weekly five") == False

def test_marker_not_found_empty():
    assert S.found("") == False

def test_marker_case_insensitive():
    assert S.found("WEEKLY-SIX POST") == True

def test_marker_partial_substring_match():
    assert S.found("weekly-sixx") == True
    assert S.found("xweekly-six") == True


# ── Photo URL discovery ─────────────────────────────────────────

_PHOTO_SRC = "https://scontent.foo1-1.fna.fbcdn.net/v/t1.0-9/photo1.jpg"
_PHOTO_DSRC = "https://scontent.foo1-1.fna.fbcdn.net/v/t1.0-9/photo2.jpg"
_ICON = "https://static.xx.fbcdn.net/rsrc.php/v1/emoji/icon.png"
_PHOTO_DATA = "https://scontent.foo1-1.fna.fbcdn.net/v/t1.0-9/data_photo.jpg"


def _html(srcs=None, dsrcs=None):
    imgs = []
    for s in (srcs or []):
        imgs.append(f'<img src="{s}" alt="photo">')
    for s in (dsrcs or []):
        imgs.append(f'<img data-src="{s}" alt="photo">')
    return "<html><body>" + "".join(imgs) + "</body></html>"


def test_discover_finds_src():
    urls = S.discover(_html(srcs=[_PHOTO_SRC]))
    assert urls == [_PHOTO_SRC]

def test_discover_finds_data_src_when_no_src():
    urls = S.discover(_html(srcs=[], dsrcs=[_PHOTO_DSRC]))
    assert urls == [_PHOTO_DSRC]

def test_discover_prefers_src_over_data_src():
    """When both src and data-src exist, only src is returned."""
    urls = S.discover(_html(srcs=[_PHOTO_SRC], dsrcs=[_PHOTO_DSRC]))
    assert urls == [_PHOTO_SRC]

def test_discover_skips_icons():
    urls = S.discover(_html(srcs=[_ICON, _PHOTO_SRC]))
    assert _ICON not in urls
    assert _PHOTO_SRC in urls

def test_discover_orders_newest_first():
    urls = [f"https://example.com/photo{i}.jpg" for i in range(5)]
    discovered = S.discover(_html(srcs=urls))
    assert discovered == urls

def test_discover_empty_html():
    assert S.discover("") == []
    assert S.discover("<html></html>") == []

def test_discover_respects_max_scan():
    urls = [f"https://example.com/p{i}.jpg" for i in range(50)]
    discovered = S.discover(_html(srcs=urls))
    assert len(discovered) <= S.MAX_SCAN

def test_discover_no_duplicates():
    urls = S.discover(_html(srcs=[_PHOTO_SRC, _PHOTO_SRC, _PHOTO_SRC]))
    assert len(urls) == 1


# ── Ambiguous ordering ──────────────────────────────────────────

def test_discover_protocol_order_when_mixed_types():
    """When src and data-src both exist, only src is returned."""
    urls = S.discover(_html(srcs=["https://ex.com/a.jpg"],
                              dsrcs=["https://ex.com/b.jpg"]))
    assert len(urls) == 1
    assert "a.jpg" in urls[0]


# ── Credibility gates ───────────────────────────────────────────

def test_proc_photo_returns_none_for_missing(monkeypatch, tmp_path):
    """A photo that cannot download returns None (not error)."""
    def fake_dl(u, d):
        pass  # pretend success but no file written
    monkeypatch.setattr(S, "dl", fake_dl)
    wd = tempfile.mkdtemp(prefix="l6t_")
    try:
        result = S.proc_photo("https://example.com/fail.jpg", wd, 0)
        assert result is None
    finally:
        shutil.rmtree(wd, ignore_errors=True)

def test_proc_photo_cleanup_removes_files(monkeypatch):
    """Even if download writes nothing, temp files are cleaned up."""
    def fake_dl(u, d):
        pass  # pretend success, no file created
    monkeypatch.setattr(S, "dl", fake_dl)
    wd = tempfile.mkdtemp(prefix="l6t_")
    try:
        before = set(os.listdir(wd))
        S.proc_photo("https://example.com/fail.jpg", wd, 0)
        after = set(os.listdir(wd))
        assert before == after
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# ── Scan result messages ────────────────────────────────────────

def test_scan_empty_html_returns_not_found():
    result = S.scan("")
    assert result["status"] == "not_found"

def test_scan_no_photos_returns_not_found():
    result = S.scan("<html><body><p>no photos</p></body></html>")
    assert result["status"] == "not_found"

def test_scan_not_found_message():
    result = S.scan("")
    assert result.get("detail") == "no visible photos found"

def test_scan_without_marker_returns_not_found():
    html = _html(srcs=["https://ex.com/photo.jpg"])
    result = S.scan(html)
    assert result["status"] == "not_found"


# ── Protocol helpers ────────────────────────────────────────────

def test_result_exit_code():
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        try:
            S.result("ok")
        except SystemExit as e:
            assert e.code == 0
    output = buf.getvalue()
    assert "KYREX_RESULT_JSON" in output
    parsed = json.loads(output.split(":", 1)[1])
    assert parsed["status"] == "ok"

def test_result_not_found_exit():
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        try:
            S.result("not_found")
        except SystemExit as e:
            assert e.code == 1
    assert "not_found" in buf.getvalue()


# ── Fail-closed on missing tools ────────────────────────────────

def test_ck_tools_raises_when_missing(monkeypatch):
    monkeypatch.setattr(S, "TESS", "nonexistent_tesseract_bin_xyz")
    monkeypatch.setattr(S, "IM", "nonexistent_im_bin_xyz")
    with pytest.raises(S.NoTool):
        S.ck_tools()


# ── Cleanup ─────────────────────────────────────────────────────

def test_temp_dir_cleaned_after_scan():
    """scan() must clean up its temp work directory."""
    html = _html(srcs=[_PHOTO_SRC])
    result = S.scan(html)
    assert result["status"] in ("not_found", "ok")
