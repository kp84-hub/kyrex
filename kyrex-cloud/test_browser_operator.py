"""Focused unit tests for browser_operator.py.

Covers: structured action parsing, the per-Bot site/domain allowlist, workspace
containment, secret redaction, session isolation, and the approval/denial
dispatch — all without a real browser (the driver is a test double).

Pytest-style and import-safe (no import-time execution) so the Cloud conftest
collects it. Run: python3 -m pytest test_browser_operator.py
"""
import json
import os
from pathlib import Path

import pytest

import browser_operator as bo


# ── Test doubles ───────────────────────────────────────────────────────

class FakeDriver:
    """Records driver calls and serves canned page content."""

    def __init__(self, body="page body", title="Title", current="",
                 raw_html=None):
        self.body = body
        self._raw_html = raw_html
        self._title = title
        self._url = current
        self.calls = []
        self.closed = False

    def open(self):
        self.calls.append(("open",))

    def navigate(self, url):
        self.calls.append(("navigate", url))
        self._url = url

    def current_url(self):
        return self._url

    def title(self):
        return self._title

    def text(self):
        return self.body

    def content(self):
        return getattr(self, "_raw_html",
                       f"<html><body>{self.body}</body></html>")

    def click(self, selector):
        self.calls.append(("click", selector))

    def type(self, selector, text):
        self.calls.append(("type", selector, text))

    def upload(self, selector, path):
        self.calls.append(("upload", selector, path))

    def download(self, url):
        self.calls.append(("download", url))
        return b"downloaded-bytes"

    def screenshot(self, path):
        self.calls.append(("screenshot", path))
        Path(path).write_bytes(b"png")

    def close(self):
        self.closed = True
        self.calls.append(("close",))

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


def root_for(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ── Spec parsing ───────────────────────────────────────────────────────

def test_parse_spec_url_shorthand_becomes_navigate_then_read():
    actions = bo.parse_spec(json.dumps({"url": "https://example.com"}))
    assert [a["action"] for a in actions] == ["navigate", "read"]


def test_parse_spec_accepts_action_list():
    actions = bo.parse_spec(json.dumps(
        {"actions": [{"action": "click", "selector": "#go"}]}
    ))
    assert actions[0]["action"] == "click"


def test_parse_spec_rejects_unknown_action():
    with pytest.raises(bo.SpecError):
        bo.parse_spec(json.dumps({"actions": [{"action": "evaluate", "script": "x"}]}))


def test_parse_spec_rejects_non_json_and_non_object():
    with pytest.raises(bo.SpecError):
        bo.parse_spec("not json")
    with pytest.raises(bo.SpecError):
        bo.parse_spec(json.dumps(["navigate"]))


def test_parse_spec_rejects_empty_actions():
    with pytest.raises(bo.SpecError):
        bo.parse_spec(json.dumps({"actions": []}))


# ── Allowlist ──────────────────────────────────────────────────────────

def test_normalize_host_variants():
    assert bo.normalize_host("Example.COM") == "example.com"
    assert bo.normalize_host("https://example.com/path") == "example.com"
    assert bo.normalize_host("example.com:8443") == "example.com"
    assert bo.normalize_host("  ") is None
    assert bo.normalize_host("bad host") is None


def test_parse_allowlist_accepts_json_and_csv():
    assert bo.parse_allowlist('["example.com", "http://a.test/"]') == [
        "example.com", "a.test"
    ]
    assert bo.parse_allowlist("example.com, sub.test") == ["example.com", "sub.test"]
    assert bo.parse_allowlist("") == []


def test_domain_allowed_exact_and_subdomain():
    ok, _ = bo.domain_allowed("https://example.com/a", ["example.com"])
    assert ok
    ok, _ = bo.domain_allowed("https://api.example.com/a", ["example.com"])
    assert ok


def test_domain_allowed_rejects_suffix_attack():
    ok, reason = bo.domain_allowed("https://example.com.evil.com/", ["example.com"])
    assert not ok
    assert "evil.com" in reason


def test_domain_allowed_rejects_non_http_and_userinfo():
    assert not bo.domain_allowed("file:///etc/passwd", ["example.com"])[0]
    assert not bo.domain_allowed("https://user:pw@example.com/", ["example.com"])[0]
    assert not bo.domain_allowed("", ["example.com"])[0]


# ── Workspace containment ──────────────────────────────────────────────

def test_resolve_safe_allows_inside(tmp_path):
    root = root_for(tmp_path)
    resolved, err = bo.resolve_safe("sub/file.txt", root)
    assert err is None
    assert resolved.startswith(str(root))


def test_resolve_safe_rejects_dotdot_escape(tmp_path):
    root = root_for(tmp_path)
    resolved, err = bo.resolve_safe("../outside.txt", root)
    assert resolved is None
    assert "escapes" in err


def test_resolve_safe_rejects_absolute_outside(tmp_path):
    root = root_for(tmp_path)
    resolved, err = bo.resolve_safe(str(tmp_path / "elsewhere" / "x"), root)
    assert resolved is None
    assert "escapes" in err


def test_resolve_safe_rejects_symlink_escape(tmp_path):
    root = root_for(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    link = root / "link.txt"
    link.symlink_to(secret)
    resolved, err = bo.resolve_safe("link.txt", root)
    assert resolved is None
    assert "escapes" in err


# ── Redaction ──────────────────────────────────────────────────────────

def test_redact_scrubs_env_secret(monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "abcdef123456789")
    out = bo.redact("the key is abcdef123456789 ok")
    assert "abcdef123456789" not in out
    assert "[redacted]" in out


def test_redact_scrubs_headers_and_tokens():
    assert "Bearer secretvalue" not in bo.redact("Authorization: Bearer secretvalue")
    assert "xyz" not in bo.redact("Cookie: sessionid=xyz") or "[redacted]" in bo.redact(
        "Cookie: sessionid=xyz"
    )
    assert "[redacted]" in bo.redact("token=deadbeef")
    assert "[redacted]" in bo.redact("password: hunter2")


def test_redact_obj_drops_sensitive_keys():
    out = bo.redact_obj({
        "authorization": "Bearer zzz",
        "nested": {"token": "abc", "ok": "visible"},
        "list": [{"cookie": "c=1"}],
    })
    assert out["authorization"] == "[redacted]"
    assert out["nested"]["token"] == "[redacted]"
    assert out["nested"]["ok"] == "visible"
    assert out["list"][0]["cookie"] == "[redacted]"


# ── Action -> operation mapping ────────────────────────────────────────

def test_consequential_click_maps_to_submit():
    assert bo.action_operation({"action": "click", "selector": "#buy"}) == "browser.submit"
    assert bo.action_operation(
        {"action": "click", "selector": "#x", "consequential": True}
    ) == "browser.submit"
    assert bo.action_operation({"action": "click", "selector": "#menu"}) == "browser.click"


def test_type_with_submit_maps_to_submit():
    assert bo.action_operation({"action": "type", "submit": True}) == "browser.submit"
    assert bo.action_operation({"action": "type"}) == "browser.type"


def test_approval_token_only_for_tier_two():
    assert bo.approval_token("browser.read", "x") == ""
    assert bo.approval_token("browser.submit", "x").startswith("SUBMIT ")
    assert bo.approval_token("browser.delete", "x").startswith("DELETE ")


# ── Dispatch: allowlist, approval, denial, containment ─────────────────

def test_run_actions_navigates_allowlisted_url(tmp_path):
    root = root_for(tmp_path)
    driver = FakeDriver()
    proto = bo.FakeProto()
    result = bo.run_actions(
        [{"action": "navigate", "url": "https://example.com/a"}],
        driver, root=root, allowlist=["example.com"], proto=proto,
    )
    assert result["status"] == "no_changes"
    assert proto.operations == ["browser.navigate"]
    assert driver.named("navigate")
    assert driver.closed


def test_run_actions_blocks_non_allowlisted_navigation(tmp_path):
    root = root_for(tmp_path)
    driver = FakeDriver()
    proto = bo.FakeProto()
    result = bo.run_actions(
        [{"action": "navigate", "url": "https://evil.com/"}],
        driver, root=root, allowlist=["example.com"], proto=proto,
    )
    assert result["status"] == "error"
    assert proto.operations == []
    assert not driver.named("navigate")


def test_run_actions_blocks_when_allowlist_empty(tmp_path):
    root = root_for(tmp_path)
    result = bo.run_actions(
        [{"action": "read"}], FakeDriver(), root=root, allowlist=[], proto=bo.FakeProto(),
    )
    assert result["status"] == "error"
    assert "allowlist" in result["errors"][0]


def test_run_actions_denial_stops_before_performing(tmp_path):
    root = root_for(tmp_path)
    driver = FakeDriver(current="https://example.com/")
    proto = bo.FakeProto(allow={"browser.navigate"})
    actions = [
        {"action": "navigate", "url": "https://example.com/"},
        {"action": "click", "selector": "#buy"},
        {"action": "read"},
    ]
    result = bo.run_actions(actions, driver, root=root,
                            allowlist=["example.com"], proto=proto)
    assert result["status"] == "error"
    assert result["errors"][0] == "browser.submit denied"
    assert proto.operations == ["browser.navigate", "browser.submit"]
    assert not driver.named("click")  # the denied action never ran


def test_run_actions_rechecks_live_origin_before_each_action(tmp_path):
    root = root_for(tmp_path)
    driver = FakeDriver(current="https://evil.com/")
    proto = bo.FakeProto()
    result = bo.run_actions(
        [{"action": "read"}], driver, root=root,
        allowlist=["example.com"], proto=proto,
    )
    assert result["status"] == "error"
    assert "left the allowlist" in result["errors"][0]
    assert proto.operations == []


def test_run_actions_rejects_upload_path_escape_before_approval(tmp_path):
    root = root_for(tmp_path)
    proto = bo.FakeProto()
    result = bo.run_actions(
        [{"action": "upload", "selector": "#f", "path": "../secret.txt"}],
        FakeDriver(current="https://example.com/"), root=root,
        allowlist=["example.com"], proto=proto,
    )
    assert result["status"] == "error"
    assert "escapes" in result["errors"][0]
    assert proto.operations == []


def test_run_actions_rejects_download_url_outside_allowlist(tmp_path):
    root = root_for(tmp_path)
    proto = bo.FakeProto()
    result = bo.run_actions(
        [{"action": "download", "url": "https://evil.com/x", "path": "x.bin"}],
        FakeDriver(current="https://example.com/"), root=root,
        allowlist=["example.com"], proto=proto,
    )
    assert result["status"] == "error"
    assert proto.operations == []


def test_run_actions_download_confined_to_workspace(tmp_path):
    root = root_for(tmp_path)
    driver = FakeDriver(current="https://example.com/")
    proto = bo.FakeProto()
    result = bo.run_actions(
        [{"action": "download", "url": "https://example.com/f", "path": "out.bin"}],
        driver, root=root, allowlist=["example.com"], proto=proto,
    )
    assert result["status"] == "ok"
    assert (root / "out.bin").read_bytes() == b"downloaded-bytes"
    assert result["browser_artifacts"] == ["out.bin"]


def test_run_actions_screenshot_confined_and_recorded(tmp_path):
    root = root_for(tmp_path)
    driver = FakeDriver(current="https://example.com/")
    result = bo.run_actions(
        [{"action": "screenshot", "path": "shots/a.png"}],
        driver, root=root, allowlist=["example.com"], proto=bo.FakeProto(),
    )
    assert result["status"] == "no_changes"
    assert (root / "shots" / "a.png").exists()
    assert result["browser_artifacts"] == [os.path.join("shots", "a.png")]


def test_run_actions_redacts_secret_in_page_text(tmp_path, monkeypatch):
    monkeypatch.setenv("PAGE_TOKEN", "sekretvalue123")
    root = root_for(tmp_path)
    driver = FakeDriver(
        body="welcome sekretvalue123 end",
        current="https://example.com/",
    )
    result = bo.run_actions(
        [{"action": "read"}], driver, root=root,
        allowlist=["example.com"], proto=bo.FakeProto(),
    )
    assert "sekretvalue123" not in result["final_response"]
    assert "[redacted]" in result["final_response"]


def test_run_actions_unknown_action_is_refused_by_parse():
    # Guards the "no arbitrary client-supplied commands" boundary.
    with pytest.raises(bo.SpecError):
        bo.parse_spec(json.dumps({"actions": [{"action": "run", "cmd": "rm -rf /"}]}))


# ── Session isolation ──────────────────────────────────────────────────

def test_session_dir_isolated_by_bot_and_owner(tmp_path):
    root = root_for(tmp_path)
    a = bo.browser_session_dir(root, "botA", "owner1")
    b = bo.browser_session_dir(root, "botB", "owner1")
    c = bo.browser_session_dir(root, "botA", "owner2")
    assert a != b != c
    assert a != c
    assert "bot-botA" in str(a)
    assert "owner-owner1" in str(a)


def test_session_dir_sanitizes_paths(tmp_path):
    root = root_for(tmp_path)
    d = bo.browser_session_dir(root, "../evil", "../../root")
    assert ".." not in str(d)


# ── Preflight ──────────────────────────────────────────────────────────

def test_preflight_blocks_empty_allowlist():
    allowed, reason = bo.preflight(json.dumps({"url": "https://example.com"}), [])
    assert not allowed
    assert "allowlist" in reason


def test_preflight_blocks_non_allowlisted_url():
    allowed, reason = bo.preflight(
        json.dumps({"url": "https://evil.com"}), ["example.com"]
    )
    assert not allowed
    assert "evil.com" in reason


def test_preflight_allows_allowlisted_url():
    allowed, reason = bo.preflight(
        json.dumps({"url": "https://example.com"}), ["example.com"]
    )
    assert allowed
    assert reason == ""


def test_preflight_rejects_bad_json():
    allowed, _ = bo.preflight("not json", ["example.com"])
    assert not allowed


# ── Level 6 weekly photos scan ────────────────────────────────────────

def test_run_actions_level6_weekly_uses_authorized_driver(monkeypatch, tmp_path):
    """run_actions navigates the EXISTING authorized driver for level6_weekly
    — never a new Playwright session. The driver is navigated, read, and the
    scanner runs only on HTML from the authorized session."""
    import types as _types
    import sys as _sys_mod

    # Mock the scanner module — intercept discover + proc_photo so no real
    # Playwright / ImageMagick / Tesseract are needed.
    _mock = _types.ModuleType("level6_photos_scanner")
    _mock.MARKER = "weekly-six"
    _mock.discover = lambda html: [
        "https://scontent.foo1-1.fna.fbcdn.net/v/t1.0-9/photo1.jpg",
    ]
    _mock.proc_photo = lambda url, wd, ix: {
        "combined": "weekly-six found in scan",
        "passes": ["weekly-six", "found"],
        "marker": True,
    }
    monkeypatch.setitem(_sys_mod.modules, "level6_photos_scanner", _mock)

    root = tmp_path / "workspace"
    root.mkdir()
    driver = FakeDriver(current="https://www.facebook.com/level6training/photos")
    proto = bo.FakeProto()
    result = bo.run_actions(
        [{"action": "level6_weekly"}],
        driver, root=root, allowlist=["facebook.com"], proto=proto,
    )
    # The scanner found the marker → result maps to "no_changes" read.
    assert result["status"] == "no_changes", f"got {result}"
    assert "weekly-six found" in result["final_response"]
    # Driver was navigated (authorized path — not an independent session).
    assert driver.named("navigate"), "driver.navigate was never called"
    assert ("navigate", "https://www.facebook.com/level6training/photos") in driver.calls


def test_run_actions_level6_weekly_allowlist_blocks(monkeypatch, tmp_path):
    """level6_weekly without facebook.com in the allowlist is blocked before
    any navigation — the allowlist gate is enforced exactly like navigate."""
    import types as _types
    import sys as _sys_mod
    _mock = _types.ModuleType("level6_photos_scanner")
    _mock.MARKER = "weekly-six"
    _mock.discover = lambda html: ["https://scontent.example.com/p.jpg"]
    _mock.proc_photo = lambda url, wd, ix: None
    monkeypatch.setitem(_sys_mod.modules, "level6_photos_scanner", _mock)

    root = tmp_path / "workspace"
    root.mkdir()
    driver = FakeDriver()
    proto = bo.FakeProto()
    result = bo.run_actions(
        [{"action": "level6_weekly"}],
        driver, root=root, allowlist=["example.com"], proto=proto,
    )
    assert result["status"] == "error"
    assert "blocked" in result["errors"][0] or "allowlist" in result["errors"][0]
    # Driver must NOT have navigated — blocked before any action.
    assert not driver.named("navigate")


def test_run_actions_level6_weekly_approval_gate(monkeypatch, tmp_path):
    """level6_weekly respects the approval gate — when proto denies the
    operation the action stops before any navigation."""
    import types as _types
    import sys as _sys_mod
    _mock = _types.ModuleType("level6_photos_scanner")
    _mock.MARKER = "weekly-six"
    _mock.discover = lambda html: ["https://scontent.example.com/p.jpg"]
    _mock.proc_photo = lambda url, wd, ix: None
    monkeypatch.setitem(_sys_mod.modules, "level6_photos_scanner", _mock)

    root = tmp_path / "workspace"
    root.mkdir()
    driver = FakeDriver()
    # Empty allow set = deny all operations.
    proto = bo.FakeProto(allow=set())
    result = bo.run_actions(
        [{"action": "level6_weekly"}],
        driver, root=root, allowlist=["facebook.com"], proto=proto,
    )
    assert result["status"] == "error"
    assert "denied" in result["errors"][0]
    # Driver must NOT have navigated — blocked by approval gate.
    assert not driver.named("navigate")


def test_run_actions_level6_weekly_not_found(monkeypatch, tmp_path):
    """When no photos carry the weekly-six marker, run_actions returns an
    error — no independent session was created."""
    import types as _types
    import sys as _sys_mod
    _mock = _types.ModuleType("level6_photos_scanner")
    _mock.MARKER = "weekly-six"
    _mock.discover = lambda html: [
        "https://scontent.foo1-1.fna.fbcdn.net/v/t1.0-9/photo1.jpg",
    ]
    # proc_photo returns no marker hit
    _mock.proc_photo = lambda url, wd, ix: {"combined": "no marker text",
                                              "passes": ["no", "marker"],
                                              "marker": False}
    monkeypatch.setitem(_sys_mod.modules, "level6_photos_scanner", _mock)

    root = tmp_path / "workspace"
    root.mkdir()
    driver = FakeDriver(current="https://www.facebook.com/level6training/photos")
    result = bo.run_actions(
        [{"action": "level6_weekly"}],
        driver, root=root,
        allowlist=["facebook.com"], proto=bo.FakeProto(),
    )
    assert result["status"] == "error"
    assert "weekly-six marker" in result["errors"][0]
    # Still used the authorized driver.
    assert driver.named("navigate")


def test_run_actions_level6_weekly_rejects_old_article_scan():
    """parse_spec refuses the old-style article_scan action — only
    level6_weekly is dispatchable, so the live path cannot bypass the
    Photos scanner."""
    with pytest.raises(bo.SpecError, match="unsupported action"):
        bo.parse_spec(json.dumps({"actions": [{"action": "article_scan"}]}))
