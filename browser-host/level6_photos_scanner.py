#!/usr/bin/env python3
"""level6_photos_scanner.py - Level 6 Facebook Photos scanner.

Navigate to pinned Level 6 Photos page, inspect visible photo
thumbnails directly (not div[role="article"] posts), process
through ImageMagick preprocessing plus dual Tesseract OCR.

Protocol: KYREX_PROGRESS:{json}  KYREX_RESULT_JSON:{json}
Exit: 0=found 1=not_found 2+=error
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

URL = "https://www.facebook.com/level6training/photos"
MARKER = "weekly-six"
MAX_SCAN = 20
TESS = os.environ.get("KYREX_TESSERACT_BIN", "tesseract")
IM = os.environ.get("KYREX_IM_CONVERT_BIN", "convert")
NAV_TO = 30
LOAD_WAIT = 3
OCR_TO = 30
TESS_LANG = "eng"
PSMS = (6, 11)

class ScanErr(Exception): pass
class NoTool(ScanErr): pass
class NoPhoto(ScanErr): pass
class BadPhoto(ScanErr): pass

_NON_A = re.compile(r"[^a-z0-9 \-]")
_WSP = re.compile(r"[ \t\n\r]+")

def canon(t):
    if not t: return ""
    return _WSP.sub(" ", _NON_A.sub(" ", t.lower())).strip()

def found(t): return MARKER in canon(t)

def prog(**kw):
    print("KYREX_PROGRESS:" + json.dumps(kw), flush=True)

def result(status, **kw):
    d = {"status": status}
    d.update(kw)
    print("KYREX_RESULT_JSON:" + json.dumps(d), flush=True)
    sys.exit(0 if status == "ok" else 1)

def ck_tools():
    for n,c in (("IM", IM), ("Tess", TESS)):
        if not shutil.which(c): raise NoTool(f"{n} bin {c!r} missing")

def prep_img(inp, out):
    try:
        r = subprocess.run([IM, inp, "-colorspace", "gray",
            "-negate", "-adaptive-threshold", "50%",
            "-density", "300", "-resize", "200%", out],
            capture_output=True, timeout=OCR_TO)
    except FileNotFoundError: raise NoTool("ImageMagick missing")
    except subprocess.TimeoutExpiredError: raise ScanErr("IM timeout")
    if r.returncode:
        e = r.stderr.decode("utf-8","replace")[:500]
        raise ScanErr(f"IM failed ({r.returncode}): {e}")

def run_tess(inp, psm):
    try:
        r = subprocess.run([TESS, inp, "stdout",
            "-l", TESS_LANG, "--psm", str(psm), "--oem", "3"],
            capture_output=True, timeout=OCR_TO)
    except FileNotFoundError: raise NoTool("Tesseract missing")
    except subprocess.TimeoutExpiredError:
        raise ScanErr(f"Tess PSM {psm} timeout")
    if r.returncode:
        raise ScanErr(f"Tess PSM {psm} failed ({r.returncode})")
    return r.stdout.decode("utf-8","replace")

def dual_tess(inp):
    ck_tools()
    raw = [run_tess(inp, p) for p in PSMS]
    c = canon(" ".join(raw))
    return {"combined": c, "passes": raw, "marker": MARKER in c}

_SKIP = re.compile(r"/emoji/|/icon/|/profile/|profile_pic|/static/")

def discover(html):
    """Extract photo image URLs from FB Photos page HTML.
    Matches src= (not data-src) first, falls back to data-src.
    """
    urls = []
    for m in re.finditer(
        r'<img(?:(?!data-src)[^>])*src="([^"]+)"[^>]*>',
        html, re.IGNORECASE):
        u = m.group(1)
        if "//" in u and not _SKIP.search(u):
            f = "https:" + u if u.startswith("//") else u
            if f.startswith("http") and f not in urls:
                urls.append(f)
                if len(urls) >= MAX_SCAN: break
    if urls:
        return urls
    for m in re.finditer(
        r'<img[^>]+data-src="([^"]+)"[^>]*>',
        html, re.IGNORECASE):
        u = m.group(1)
        if "//" in u and not _SKIP.search(u):
            f = "https:" + u if u.startswith("//") else u
            if f.startswith("http") and f not in urls:
                urls.append(f)
                if len(urls) >= MAX_SCAN: break
    return urls

def dl(u, d):
    import urllib.request
    try: urllib.request.urlretrieve(u, d)
    except Exception as e: raise BadPhoto(f'dl {u[:80]}: {e}')

def proc_photo(url, wd, ix):
    rp = os.path.join(wd, f"raw_{ix:03d}.png")
    pp = os.path.join(wd, f"prep_{ix:03d}.png")
    try:
        dl(url, rp)
        prog(photo=f"proc {ix}", url=url[:80])
        if not os.path.isfile(rp) or os.path.getsize(rp) < 256:
            return None
        prep_img(rp, pp)
        if not os.path.isfile(pp): return None
        return dual_tess(pp)
    except ScanErr: raise
    except Exception as e: raise BadPhoto(f"photo {ix}: {e}")
    finally:
        for p in (rp, pp):
            try: os.unlink(p)
            except: pass

def scan(html=None):
    if html is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return {"status": "error", "error": "Playwright missing"}
        prog(phase="navigate", url=URL)
        try:
            with sync_playwright() as pw:
                b = pw.chromium.launch(headless=True)
                c = b.new_context()
                p = c.new_page()
                p.goto(URL, timeout=NAV_TO * 1000)
                time.sleep(LOAD_WAIT)
                html = p.content()
                b.close()
        except Exception as e:
            return {"status": "error", "error": f"nav: {e}"[:500]}
    urls = discover(html or "")
    if not urls:
        return {"status": "not_found",
                "detail": "no visible photos found"}
    wd = tempfile.mkdtemp(prefix="l6s_")
    try:
        for i, u in enumerate(urls[:MAX_SCAN]):
            try:
                ocr = proc_photo(u, wd, i)
            except (ScanErr, NoTool) as e:
                prog(warn=f"photo {i}: {e}"[:200])
                continue
            if ocr and ocr["marker"]:
                return {"status": "ok",
                        "detail": "weekly-six found in Level 6 photos",
                        "ocr_summary": ocr["combined"][:300]}
        return {"status": "not_found",
                "detail": "no recent visible post carried the weekly-six marker"}
    finally:
        shutil.rmtree(wd, ignore_errors=True)

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--page-html", help="HTML file for testing")
    args = ap.parse_args()
    if args.page_html:
        with open(args.page_html) as f:
            result(**scan(f.read()))
    else:
        result(**scan())

if __name__ == "__main__":
    main()
