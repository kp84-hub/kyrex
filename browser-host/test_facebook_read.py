"""Feed/OCR regressions with DOM handles, without network or a real account."""
from pathlib import Path
from types import SimpleNamespace
import pytest
import facebook_read as fb

URL = 'https://www.facebook.com/level6training/'


class Link:
    def __init__(self, href): self.href = href
    def get_attribute(self, name): return self.href


class Image:
    def __init__(self, source): self.source = source; self.paths = []; self.on_capture = None
    def is_visible(self): return True
    def bounding_box(self): return {'width': 600, 'height': 500}
    def get_attribute(self, name): return self.source
    def screenshot(self, *, path, timeout):
        self.paths.append(path)
        Path(path).write_bytes(b'pixels')
        if self.on_capture: self.on_capture()


class Article:
    def __init__(self, caption, link, image):
        self.caption = caption; self.link = link; self.image = image
    def is_visible(self): return True
    def inner_text(self): return self.caption
    def query_selector_all(self, selector):
        return [Link(self.link)] if selector == 'a[href]' else [self.image]


class Page:
    def __init__(self, batches):
        self.url = URL; self.batches = batches; self.turn = 0; self.scrolls = 0
        self.mouse = SimpleNamespace(wheel=self.wheel)
    def wheel(self, x, y): self.scrolls += 1; self.turn = min(self.turn + 1, len(self.batches)-1)
    def wait_for_timeout(self, ms): pass
    def locator(self, selector):
        return SimpleNamespace(element_handles=lambda: self.batches[self.turn])


def article(index):
    return Article(f'caption {index}', f'/level6training/posts/pfbid{index}', Image(f'image{index}'))


def test_scroll_reaches_third_post_and_keeps_ocr_with_its_link():
    posts = [article(i) for i in range(3)]
    page = Page([[posts[0]], [posts[1]], [posts[2]]])
    def ocr(path, **kwargs):
        assert Path(path).exists()
        return ('WEEK OF 09.28.26\nMONDAY ABS & GLUTES', False)
    result = fb.read_feed(page, allowed=lambda url: True, ocr=ocr)
    assert 'caption 2' in result
    assert URL.rstrip('/') + '/posts/pfbid2' in result
    assert result.count('WEEK OF 09.28.26') == 3
    assert 'fallible' in result and 'not exhaustive' in result
    assert page.scrolls <= fb.MAX_SCROLLS
    assert all(not Path(path).exists() for post in posts for path in post.image.paths)


def test_rebound_post_is_not_associated_with_captured_pixels():
    post = article(1)
    post.image.on_capture = lambda: setattr(post, 'caption', 'replacement')
    result = fb.read_feed(Page([[post]]), allowed=lambda url: True,
                          ocr=lambda *a, **k: ('BAD SCHEDULE', False))
    assert 'BAD SCHEDULE' not in result
    assert 'could not be read reliably' in result
    assert all(not Path(path).exists() for path in post.image.paths)


def test_redirect_during_capture_fails_and_deletes_pixels():
    post = article(1); page = Page([[post]])
    post.image.on_capture = lambda: setattr(page, 'url', 'https://evil.example/')
    with pytest.raises(RuntimeError, match='document changed'):
        fb.read_feed(page, allowed=lambda url: url == URL, ocr=lambda *a, **k: ('BAD', False))
    assert all(not Path(path).exists() for path in post.image.paths)


def test_ocr_failure_cleans_temporary_images_and_reports_limitation():
    post = article(1)
    def broken(*a, **k): raise RuntimeError('engine unavailable')
    result = fb.read_feed(Page([[post]]), allowed=lambda url: True, ocr=broken)
    assert 'could not be read reliably' in result
    assert all(not Path(path).exists() for path in post.image.paths)


def test_scan_cannot_exceed_post_budget():
    posts = [article(i) for i in range(20)]
    called = []
    def ocr(*args, **kwargs): called.append(1); return ('READABLE', False)
    fb.read_feed(Page([posts]), allowed=lambda url: True, ocr=ocr)
    assert len(called) == fb.MAX_POSTS


def test_truncated_ocr_is_unverified_not_partial_evidence():
    result = fb.read_feed(Page([[article(1)]]), allowed=lambda url: True,
                          ocr=lambda *a, **k: ('PARTIAL SCHEDULE', True))
    assert 'PARTIAL SCHEDULE' not in result
    assert 'exceeded its limit' in result


def test_links_are_observed_and_photo_links_not_claimed_as_post_permalinks():
    link, kind = fb.observed_post_link(['/photo/?fbid=123'], URL)
    assert link == 'https://www.facebook.com/photo/?fbid=123'
    assert 'post permalink unavailable' in kind
    assert not fb.observed_post_link(['https://facebook.com.evil.com/posts/123'], URL)[0]
    assert not fb.observed_post_link(['https://user:pass@www.facebook.com/posts/123'], URL)[0]
    link, kind = fb.observed_post_link(['/photo/?fbid=123', '/level6training/posts/456'], URL)
    assert link.endswith('/posts/456') and kind.startswith('Post link')


@pytest.mark.parametrize('url', ['https://www.facebook.com/', 'https://www.facebook.com/login',
    'https://www.facebook.com/groups/123', 'https://www.facebook.com/messages/',
    'https://facebook.com.evil.com/level6training/', 'https://www.facebook.com/checkpoint'])
def test_non_page_surfaces_are_not_scanned(url):
    page = Page([]); page.url = url
    assert fb.read_feed(page, allowed=lambda url: True) == ''
