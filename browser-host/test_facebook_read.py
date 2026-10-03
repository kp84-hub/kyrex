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


PHOTO = 'https://www.facebook.com/photo/?fbid=1724403506361239'
PHOTO_ID = '1724403506361239'


class ViewerImage(Image):
    def __init__(self, source, href='', loaded=True):
        super().__init__(source)
        self.href = href
        self.loaded = loaded
    def evaluate(self, script):
        return {'src': self.source, 'href': self.href, 'photoId': '', 'loaded': self.loaded}


class ViewerPage(Page):
    def __init__(self, images):
        self.url = PHOTO
        self.images = images
        self.waits = 0
        self.on_wait = None
    def locator(self, selector):
        if selector == 'img':
            return SimpleNamespace(element_handles=lambda: self.images)
        if selector == 'body':
            return SimpleNamespace(inner_text=lambda **kw: 'Gonna be a great week!')
        raise AssertionError('viewer must not depend on article/dialog containers')
    def wait_for_timeout(self, ms):
        self.waits += 1
        if self.on_wait: self.on_wait()


def viewer_read(page, ocr=None, allowed=None):
    return fb.read_feed(page, allowed=allowed or (lambda url: url == PHOTO),
                        ocr=ocr or (lambda *a, **k: ('WEEK OF 09.28.26\nMONDAY ABS & GLUTES', False)))


def selected_image():
    return ViewerImage(f'https://scontent.example.fbcdn.net/v/t39.30808-6/123_{PHOTO_ID}_456_n.jpg?token=private')


def test_viewer_image_outside_articles_is_read_after_loading_and_cover_is_ignored():
    cover = ViewerImage('https://scontent.example.fbcdn.net/cover_777.jpg')
    selected = selected_image(); selected.loaded = False
    page = ViewerPage([cover, selected])
    page.on_wait = lambda: setattr(selected, 'loaded', True)
    result = viewer_read(page)
    assert page.waits == 1
    assert 'WEEK OF 09.28.26' in result
    assert PHOTO in result
    assert 'not a verified post permalink' in result
    assert 'Gonna be a great week!' in result
    assert 'private' not in result and 'fbcdn' not in result
    assert not cover.paths
    assert all(not Path(path).exists() for path in selected.paths)


def test_viewer_never_substitutes_unrelated_image_with_matching_caption():
    cover = ViewerImage('https://scontent.example.fbcdn.net/cover_777.jpg')
    page = ViewerPage([cover])
    result = viewer_read(page)
    assert not cover.paths
    assert 'could not be matched' in result
    assert 'WEEK OF' not in result
    assert page.waits == fb.VIEWER_ATTEMPTS - 1


def test_viewer_exact_photo_id_not_numeric_substring():
    image = ViewerImage(f'https://scontent.example.fbcdn.net/{PHOTO_ID}99_n.jpg')
    assert not fb._viewer_image_identity(image, PHOTO_ID, PHOTO)
    image.source = f'https://untrusted.example/{PHOTO_ID}_n.jpg'
    assert not fb._viewer_image_identity(image, PHOTO_ID, PHOTO)


def test_viewer_observed_enclosing_photo_link_can_bind_rendered_image():
    image = ViewerImage('https://scontent.example.fbcdn.net/image.jpg', href=PHOTO)
    assert fb._viewer_image_identity(image, PHOTO_ID, PHOTO)
    image.href = 'https://facebook.com.evil.example/photo/?fbid=' + PHOTO_ID
    assert not fb._viewer_image_identity(image, PHOTO_ID, PHOTO)


def test_viewer_image_replaced_during_capture_is_rejected_and_deleted():
    image = selected_image()
    image.on_capture = lambda: setattr(image, 'source', 'https://scontent.example.fbcdn.net/other_777.jpg')
    result = viewer_read(ViewerPage([image]))
    assert 'changed during capture' in result and 'WEEK OF' not in result
    assert all(not Path(path).exists() for path in image.paths)


def test_viewer_image_replaced_during_ocr_is_rejected_and_deleted():
    image = selected_image()
    def ocr(*args, **kwargs):
        image.source = 'https://scontent.example.fbcdn.net/other_777.jpg'
        return 'WRONG WEEK', False
    result = viewer_read(ViewerPage([image]), ocr=ocr)
    assert 'changed during OCR' in result and 'WRONG WEEK' not in result
    assert all(not Path(path).exists() for path in image.paths)


def test_viewer_rotating_signed_query_does_not_change_image_identity():
    image = selected_image()
    image.on_capture = lambda: setattr(image, 'source', image.source.split('?')[0] + '?token=rotated')
    result = viewer_read(ViewerPage([image]))
    assert 'WEEK OF 09.28.26' in result
    assert 'rotated' not in result


def test_viewer_redirect_during_capture_fails_closed_and_deletes_pixels():
    image = selected_image(); page = ViewerPage([image])
    image.on_capture = lambda: setattr(page, 'url', 'https://www.facebook.com/photo/?fbid=777')
    with pytest.raises(RuntimeError, match='document changed'):
        viewer_read(page)
    assert all(not Path(path).exists() for path in image.paths)


def test_viewer_denied_page_is_not_captured():
    image = selected_image()
    with pytest.raises(RuntimeError, match='document changed'):
        viewer_read(ViewerPage([image]), allowed=lambda url: False)
    assert not image.paths


def test_viewer_multiple_distinct_matching_images_are_ambiguous():
    first = selected_image()
    second = ViewerImage(f'https://scontent.example.fbcdn.net/another_{PHOTO_ID}_n.jpg')
    result = viewer_read(ViewerPage([first, second]))
    assert 'ambiguous' in result
    assert not first.paths and not second.paths


def test_viewer_ocr_failure_keeps_source_but_flags_unread_image_and_deletes_pixels():
    image = selected_image()
    def ocr(*args, **kwargs): raise RuntimeError('engine failed')
    result = viewer_read(ViewerPage([image]), ocr=ocr)
    assert PHOTO in result and 'OCR failed' in result
    assert all(not Path(path).exists() for path in image.paths)


def test_viewer_without_unique_numeric_id_does_not_capture_any_image():
    page = ViewerPage([selected_image()]); page.url = PHOTO + '&fbid=777'
    result = viewer_read(page, allowed=lambda url: True)
    assert 'no unique numeric photo ID' in result
    assert not page.images[0].paths
