"""Bounded Facebook feed evidence for ordinary browser.read operations.

Only rendered post elements are read. Scrolling is internal; no clicks, URL
variants, network image downloads, or persistent image artifacts are used.
OCR is fallible evidence and never proves a post's date or page ownership.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qs

MAX_POSTS = 6
MAX_SCROLLS = 4
MAX_SECONDS = 45
FACEBOOK_HOSTS = {"facebook.com", "www.facebook.com", "web.facebook.com",
                  "m.facebook.com"}
VIEWER_ATTEMPTS = 8
VIEWER_PAUSE_MS = 500


def selected_photo_id(url):
    if not is_facebook_page(url):
        return ""
    parsed = urlsplit(url)
    if parsed.path.rstrip("/") not in {"/photo", "/photo.php"}:
        return ""
    values = parse_qs(parsed.query).get("fbid", [])
    return values[0] if len(values) == 1 and values[0].isdigit() else ""


def _viewer_image_identity(image, photo_id, original):
    """Bind displayed pixels to a selected photo, never to a page cover.

    Facebook may render a viewer image outside article/dialog containers. A
    visible, decoded image needs an exact photo ID in its enclosing photo
    link/data attribute or rendered CDN filename, plus a stable image path.
    Signed CDN queries are neither evidence nor part of the fingerprint.
    """
    if not image.is_visible():
        return ""
    box = image.bounding_box()
    if not box or box["width"] < 180 or box["height"] < 120:
        return ""
    data = image.evaluate("""el => ({
        src: el.currentSrc || el.getAttribute('src') || '',
        href: el.closest('a[href]')?.href || '',
        photoId: el.closest('[data-fbid]')?.getAttribute('data-fbid') || '',
        loaded: el.complete && el.naturalWidth > 0 && el.naturalHeight > 0
    })""")
    if not isinstance(data, dict) or not data.get("loaded"):
        return ""
    try:
        source = urlsplit(str(data.get("src") or ""))
        source_path = source.path
        source_host = source.hostname or ""
        filename_matches = (source_host == "fbcdn.net" or source_host.endswith(".fbcdn.net")) and bool(
            re.search(r"(?<!\d)" + re.escape(photo_id) + r"(?!\d)", source_path))
        observed_link = urljoin(original, str(data.get("href") or "")) if data.get("href") else ""
        bound = (selected_photo_id(observed_link) == photo_id
                 or str(data.get("photoId") or "") == photo_id or filename_matches)
        if not bound or not source_path:
            return ""
        return hashlib.sha256((source_host + source_path).encode()).hexdigest()
    except ValueError:
        return ""


def _ocr_text(path, ocr, deadline):
    try:
        text, truncated = ocr(path, psm="11", timeout=min(
            6.0, max(0.1, deadline - time.monotonic())), max_bytes=6000)
    except Exception:
        return "Image OCR failed or timed out; content unverified."
    if truncated:
        return "Image OCR exceeded its limit; content unverified."
    if not text.strip():
        return "Image OCR returned no readable text; content unverified."
    return ("Image OCR (fallible; verify the printed week/date before claiming "
            "the requested week was found):\n" + text.strip()[:1800])


def read_selected_photo(page, *, allowed, ocr, deadline):
    """Read only the image bound to the numeric fbid in the current URL."""
    original = str(page.url)
    photo_id = selected_photo_id(original)

    def check():
        if str(page.url) != original or not allowed(str(page.url)):
            raise RuntimeError("Facebook document changed during photo read")

    for attempt in range(VIEWER_ATTEMPTS):
        check()
        matches = []
        for image in page.locator("img").element_handles()[:100]:
            try:
                identity = _viewer_image_identity(image, photo_id, original)
                if identity:
                    matches.append((identity, image))
            except Exception:
                continue
        check()
        identities = {identity for identity, _ in matches}
        if len(identities) > 1:
            return "Selected Facebook photo image is ambiguous; image content unverified."
        if matches:
            identity, image = matches[0]
            try:
                with tempfile.TemporaryDirectory(prefix="kyrex-fb-photo-") as temp:
                    path = Path(temp) / "image.png"
                    image.screenshot(path=str(path), timeout=2000)
                    check()
                    if _viewer_image_identity(image, photo_id, original) != identity:
                        return "Selected photo changed during capture; image content unverified."
                    evidence = _ocr_text(path, ocr, deadline)
                    check()
                    if _viewer_image_identity(image, photo_id, original) != identity:
                        return "Selected photo changed during OCR; image content unverified."
                    caption = page.locator("body").inner_text(timeout=1000)[:1200]
                    check()
                    return "\n".join([
                        "Selected Facebook photo (image identity matched; printed week still needs verification).",
                        "Photo viewer source (not a verified post permalink): " + original,
                        "Viewer page text (may include controls/comments): " + caption,
                        evidence])
            except Exception:
                check()
                return "Selected photo capture failed; image content unverified."
        if attempt + 1 < VIEWER_ATTEMPTS and time.monotonic() < deadline:
            page.wait_for_timeout(VIEWER_PAUSE_MS)
        else:
            break
    return ("Selected Facebook photo image did not load or could not be matched to "
            "the requested photo ID; image content unverified. Other page images "
            "were not substituted. A matching caption alone does not verify the week.")


def is_facebook_page(url):
    try:
        parsed = urlsplit(url)
        path = parsed.path.strip("/").lower()
        return (parsed.scheme == "https" and parsed.hostname in FACEBOOK_HOSTS
                and not parsed.username and not parsed.password and bool(path)
                and path.split("/")[0] not in {
                    "login", "checkpoint", "messages", "settings", "groups",
                    "friends", "home.php", "recover", "consent"})
    except ValueError:
        return False


def observed_post_link(hrefs, page_url):
    """Return only an observed Facebook post/photo URL, never construct an ID."""
    photos = []
    for href in hrefs[:100]:
        try:
            url = urljoin(page_url, str(href or ""))
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname not in FACEBOOK_HOSTS
                    or parsed.username or parsed.password or len(url) > 1000):
                continue
            query = parse_qs(parsed.query)
            if "/posts/" in parsed.path or "story_fbid" in query:
                return url, "Post link observed in this article"
            if parsed.path.rstrip("/") in {"/photo", "/photo.php"} and "fbid" in query:
                photos.append(url)
        except ValueError:
            continue
    if photos:
        return photos[0], "Photo link observed in this article (post permalink unavailable)"
    return "", "Post permalink unavailable"


def _snapshot(article, page_url):
    caption = article.inner_text()[:1800]
    hrefs = [a.get_attribute("href") for a in article.query_selector_all("a[href]")[:100]]
    link, kind = observed_post_link(hrefs, page_url)
    # Use a pinned DOM handle for the largest content image, excluding avatars.
    images = article.query_selector_all("img")[:20]
    candidates = []
    for image in images:
        if image.is_visible():
            box = image.bounding_box()
            if box and box["width"] >= 180 and box["height"] >= 120:
                candidates.append((box["width"] * box["height"], image))
    image = max(candidates, key=lambda row: row[0])[1] if candidates else None
    image_id = image.get_attribute("src") if image else ""
    identity = (caption, link, image_id)
    # Deduplicate by observed link (or image) so a post that rebinds while
    # captured is not silently retried as a new article later in the scan.
    key = hashlib.sha256(str(link or image_id or identity).encode()).hexdigest()
    return identity, key, kind, image


def read_feed(page, *, allowed, ocr=None):
    """Scan up to six rendered articles across four scrolls, within 45 seconds.

    The original document URL must remain unchanged and allowed at every
    snapshot/capture boundary. A pinned article/image must still match after
    capture; a re-render is reported rather than attached to another post.
    """
    original = str(page.url)
    if not is_facebook_page(original):
        return ""
    deadline = time.monotonic() + MAX_SECONDS
    records, seen = [], set()
    attempts = 0
    note = ""

    def check_page():
        if str(page.url) != original or not allowed(str(page.url)):
            raise RuntimeError("Facebook document changed during feed read")

    if ocr is None:
        from level6_post import run_ocr
        ocr = run_ocr
    if urlsplit(original).path.rstrip("/") in {"/photo", "/photo.php"}:
        if not selected_photo_id(original):
            return "Photo viewer has no unique numeric photo ID; image content unverified."
        return read_selected_photo(page, allowed=allowed, ocr=ocr, deadline=deadline)
    for turn in range(MAX_SCROLLS + 1):
        check_page()
        selector = '[role="article"]'
        if urlsplit(original).path.rstrip("/") in {"/photo", "/photo.php"}:
            selector = '[role="article"], [role="dialog"]'
        articles = page.locator(selector).element_handles()[:20]
        for article in articles:
            if attempts >= MAX_POSTS or time.monotonic() >= deadline:
                break
            try:
                if not article.is_visible():
                    continue
                identity, key, kind, image = _snapshot(article, original)
                if key in seen:
                    continue
                seen.add(key)
                attempts += 1
                check_page()
                caption, link, _ = identity
                image_text = "No large rendered image available; image content unread."
                if image is not None:
                    # TemporaryDirectory deletes pixels on success and failure.
                    with tempfile.TemporaryDirectory(prefix="kyrex-fb-read-") as temp:
                        path = Path(temp) / "image.png"
                        image.screenshot(path=str(path), timeout=2000)
                        check_page()
                        current, _, _, _ = _snapshot(article, original)
                        if current != identity:
                            raise RuntimeError("Post changed during image capture")
                        try:
                            text, truncated = ocr(path, psm="11", timeout=min(
                                6.0, max(0.1, deadline - time.monotonic())), max_bytes=6000)
                        except Exception:
                            text, truncated = "", False
                            note = "One or more posts/images could not be read reliably."
                        if truncated:
                            image_text = "Image OCR exceeded its limit; content unverified."
                        elif text.strip():
                            image_text = ("Image OCR (fallible; flag uncertain words and verify "
                                          "the printed week/date):\n" + text.strip()[:1800])
                        else:
                            image_text = "Image OCR returned no readable text; content unverified."
                check_page()
                current, _, _, _ = _snapshot(article, original)
                if current != identity:
                    raise RuntimeError("Post changed during image read")
                records.append("\n".join([
                    f"Rendered article {attempts} (feed order is not verified chronology)",
                    kind + (": " + link if link else ""),
                    "Caption/page text: " + caption[:800], image_text]))
            except Exception:
                check_page()  # Redirects fail the entire read, not just one image.
                note = "One or more posts/images could not be read reliably."
        if attempts >= MAX_POSTS or time.monotonic() >= deadline or turn == MAX_SCROLLS:
            break
        check_page()
        page.mouse.wheel(0, 1000)
        page.wait_for_timeout(600)
    check_page()
    result = "\n\n".join(filter(None, [
        "Facebook feed sample (bounded; not exhaustive). Each OCR block belongs "
        "only to its article. Do not combine schedules across articles or infer "
        "cancellation status from a missing notice.",
        *records,
        note,
        "No rendered post articles available; feed/image content unverified." if not records else "",
    ]))
    if len(result) > 10000:
        result = result[:9900] + "\nEvidence truncated; remaining articles/text unverified."
    return result
