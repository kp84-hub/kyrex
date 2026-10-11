"""Exercise viewer capture against local HTML in a real Chromium browser.

Run with KYREX_TEST_BROWSER=1 after installing Playwright and Chromium.
All requests are fulfilled locally; no Facebook account or network is used.
"""
import os
from pathlib import Path
import struct
import sys
import zlib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kyrex-cloud"))
import browser_operator as bo

pytestmark = pytest.mark.skipif(
    os.environ.get("KYREX_TEST_BROWSER") != "1",
    reason="requires an installed Chromium browser",
)

PHOTO_URL = "https://www.facebook.com/photo/?fbid=123456"
IMAGE_URL = "https://www.facebook.com/weekly-six.png"


def png(width=800, height=800):
    def chunk(kind, data):
        return (struct.pack("!I", len(data)) + kind + data
                + struct.pack("!I", zlib.crc32(kind + data)))
    pixels = (b"\x00" + b"\x20\x80\xe0" * width) * height
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


class Source:
    def get_attribute(self, name):
        return IMAGE_URL if name == "src" else None


@pytest.fixture
def viewer():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 1000})
        yield context
        context.close()
        browser.close()


def serve(context, *, script="", moving=False):
    animation = "animation: drift 1s linear infinite;" if moving else ""
    markup = f"""<style>
      @keyframes drift {{ from {{ transform: translateX(0); }}
                           to {{ transform: translateX(200px); }} }}
      img {{ width: 600px; height: 600px; max-width: 600px;
             max-height: 600px; {animation} }}
    </style><img id="selected" src="{IMAGE_URL}"><script>{script}</script>"""
    def route(request):
        if request.request.url == PHOTO_URL:
            request.fulfill(content_type="text/html", body=markup)
        elif request.request.url in {IMAGE_URL, IMAGE_URL + "?changed=1",
                                     "https://www.facebook.com/other.png"}:
            request.fulfill(content_type="image/png", body=png())
        else:
            request.abort()
    context.route("**/*", route)


def capture(context, tmp_path):
    driver = bo.PlaywrightDriver(tmp_path)
    driver._context = context
    return driver._capture_photo_viewer(
        PHOTO_URL, str(tmp_path / "photo.png"),
        expected_key=bo._level6_photo_key(Source()))


def test_moving_selected_photo_captures_at_natural_size(viewer, tmp_path):
    from playwright.sync_api import TimeoutError
    serve(viewer, moving=True)
    control = viewer.new_page()
    control.goto(PHOTO_URL)
    # Reproduce the old failure: default screenshots wait forever for a
    # moving image to become stable, despite its pixels already being loaded.
    with pytest.raises(TimeoutError, match="stable"):
        control.locator("#selected").screenshot(timeout=750)
    control.close()
    info = capture(viewer, tmp_path)
    assert info == {"width": 800, "height": 800, "capture_mode": "photo_viewer"}
    image = (tmp_path / "photo.png").read_bytes()
    assert struct.unpack("!II", image[16:24]) == (800, 800)
    assert not viewer.pages


def test_viewer_selection_keeps_same_node_after_img_reorder(viewer):
    serve(viewer)
    page = viewer.new_page()
    page.goto(PHOTO_URL)
    selected = bo._find_level6_photo_viewer_image(
        page, expected_key=bo._level6_photo_key(Source()))
    assert selected is not None
    page.evaluate("""() => {
      const other = document.createElement('img');
      other.src = 'https://www.facebook.com/other.png';
      document.body.prepend(other);
    }""")
    assert selected.get_attribute("id") == "selected"
    assert bo._level6_photo_key(selected) == bo._level6_photo_key(Source())
    page.close()


@pytest.mark.parametrize("change", [
    "el.src = 'https://www.facebook.com/other.png'",
    "history.replaceState(null, '', '/photo/?fbid=999')",
])
def test_photo_or_document_change_during_preparation_is_rejected(viewer, tmp_path, change):
    serve(viewer, script=f"""
      const el = document.getElementById('selected');
      new MutationObserver((records, observer) => {{
        observer.disconnect(); {change};
      }}).observe(el, {{attributes: true, attributeFilter: ['style']}});
    """)
    with pytest.raises(bo.DriverError, match="selected viewer photo changed"):
        capture(viewer, tmp_path)
    assert not (tmp_path / "photo.png").exists()
    assert not viewer.pages


def test_signed_query_rotation_during_preparation_keeps_identity(viewer, tmp_path):
    serve(viewer, script="""
      const el = document.getElementById('selected');
      new MutationObserver((records, observer) => {
        observer.disconnect(); el.src += '?changed=1';
      }).observe(el, {attributes: true, attributeFilter: ['style']});
    """)
    assert capture(viewer, tmp_path)["capture_mode"] == "photo_viewer"
    assert (tmp_path / "photo.png").exists()
    assert not viewer.pages


def test_photo_changed_by_screenshot_animation_event_is_deleted(viewer, tmp_path):
    # Disabling a finite animation fires its completion event. A viewer
    # reacting by replacing the selected source must not reach OCR.
    serve(viewer, script="""
      const el = document.getElementById('selected');
      el.style.animation = 'drift 60s linear';
      el.addEventListener('animationend', () => {
        el.src = 'https://www.facebook.com/other.png';
      }, {once: true});
    """)
    with pytest.raises(bo.DriverError, match="selected viewer photo changed"):
        capture(viewer, tmp_path)
    assert not (tmp_path / "photo.png").exists()
    assert not viewer.pages
