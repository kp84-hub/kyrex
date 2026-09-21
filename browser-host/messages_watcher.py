"""Watch a fixed Google Messages group for the exact ``#L6Workout`` trigger."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import manual_mode
import profiles

TRIGGER = "#L6Workout"
POLL_SECONDS = 3.0
SEND_WINDOW_SECONDS = 600.0


def exact_trigger(value) -> bool:
    return str(value or "").strip() == TRIGGER


def trigger_fingerprint(identity: str) -> str:
    return hashlib.sha256(str(identity or "").encode()).hexdigest()


def cloud_trigger_url(ws_url: str) -> str:
    parsed = urlsplit(str(ws_url or "").strip())
    if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
        raise ValueError("invalid Cloud websocket URL")
    return urlunsplit(("https" if parsed.scheme == "wss" else "http",
                       parsed.netloc,
                       "/api/browser-hosts/google-messages-trigger", "", ""))


def _receipt_path(profile_dir: Path) -> Path:
    return profile_dir / ".kyrex-google-messages-inbound.json"


def _read_receipts(profile_dir: Path) -> set[str]:
    try:
        data = json.loads(_receipt_path(profile_dir).read_text("utf-8"))
        return {str(v) for v in data.get("triggers", []) if str(v)}
    except (OSError, ValueError, TypeError):
        return set()


def _write_receipts(profile_dir: Path, receipts: set[str]) -> None:
    path = _receipt_path(profile_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"triggers": sorted(receipts)[-100:]}) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _submit(config: dict, fingerprint: str) -> bool:
    nonce = f"{int(time.time())}.{secrets.token_hex(16)}"
    proof = hmac.new(config["secret"].encode(),
                     f"{config['host_id']}.{nonce}".encode(),
                     hashlib.sha256).hexdigest()
    body = json.dumps({"host_id": config["host_id"], "nonce": nonce,
                       "proof": proof, "trigger_id": fingerprint}).encode()
    request = urllib.request.Request(
        cloud_trigger_url(config["cloud_url"]), data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("status") in {"queued", "duplicate"}
    except (OSError, ValueError, urllib.error.HTTPError):
        return False


def _latest_trigger(page):
    """Return an opaque DOM identity for the newest exact trigger, or None."""
    return page.evaluate("""
    (trigger) => {
      const selectors = [
        'mws-message-wrapper', '[data-message-id]', '[data-e2e-message-id]',
        '[role="listitem"]', 'mws-message-part-content'
      ];
      const nodes = Array.from(document.querySelectorAll(selectors.join(',')));
      const hits = [];
      for (const node of nodes) {
        if ((node.innerText || node.textContent || '').trim() !== trigger) continue;
        const container = node.closest(
          'mws-message-wrapper,[data-message-id],[data-e2e-message-id],[role="listitem"]'
        ) || node;
        hits.push([
          container.getAttribute('data-message-id') || '',
          container.getAttribute('data-e2e-message-id') || '',
          container.getAttribute('aria-label') || '',
          container.outerHTML.slice(0, 2000)
        ].join('|'));
      }
      return hits.length ? hits[hits.length - 1] : null;
    }
    """, TRIGGER)


def _open_conversation(page, url: str) -> None:
    """Open the fixed Messages conversation without waiting on SPA load.

    Google Messages is service-worker driven and can leave
    ``domcontentloaded`` pending even after the usable page has committed.
    Navigation therefore waits only for the document commit. The existing
    bounded poll below is the authoritative UI-readiness check.
    """
    try:
        page.goto(url, wait_until="commit", timeout=30000)
    except Exception as exc:
        # Messages can keep Playwright's navigation lifecycle pending even
        # after Chromium has reached its service-worker page. Recover only
        # when the browser demonstrably remains on Google's fixed Messages
        # web surface; about:blank, redirects, and foreign hosts still fail.
        current = urlsplit(str(getattr(page, "url", "") or ""))
        safe_messages_page = (
            current.scheme == "https"
            and current.netloc == "messages.google.com"
            and current.path.startswith("/web/")
        )
        if type(exc).__name__ != "TimeoutError" or not safe_messages_page:
            raise
    page.wait_for_timeout(2000)
    if "/welcome" in page.url:
        raise RuntimeError("Google Messages pairing is not active")


def run() -> None:
    config = {
        "host_id": os.environ.get("KYREX_HOST_ID", "").strip(),
        "owner": os.environ.get("KYREX_HOST_OWNER", "").strip(),
        "secret": os.environ.get("KYREX_HOST_ENROLLMENT_SECRET", "").strip(),
        "cloud_url": os.environ.get("KYREX_HOST_CLOUD_URL", "").strip(),
        "bot_id": os.environ.get("KYREX_MESSAGES_BOT_ID", "calendar").strip(),
        "url": os.environ.get("KYREX_GOOGLE_MESSAGES_CONVERSATION_URL", "").strip(),
        "executable": os.environ.get("KYREX_BROWSER_EXECUTABLE", "/usr/bin/chromium"),
        "profiles_root": os.environ.get("KYREX_BROWSER_PROFILES_ROOT", "/profiles"),
    }
    required = ("host_id", "owner", "secret", "cloud_url", "bot_id", "url")
    if any(not config[k] for k in required):
        raise SystemExit("[messages-watcher] required configuration is missing")
    if not config["url"].startswith("https://messages.google.com/web/conversations/"):
        raise SystemExit("[messages-watcher] fixed conversation URL is invalid")

    profile_dir = profiles.ensure_profile(config["owner"], config["bot_id"],
                                          root=config["profiles_root"])
    initialized = _receipt_path(Path(profile_dir)).exists()
    receipts = _read_receipts(Path(profile_dir))
    state_root = manual_mode.state_dir(profiles_root=config["profiles_root"])

    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        while True:
            lock = None
            context = None
            stage = "acquire"
            try:
                lock = manual_mode.acquire(
                    config["owner"], config["bot_id"],
                    kind=manual_mode.KIND_AUTOMATION,
                    ttl=manual_mode.AUTOMATION_MAX_TTL, root=state_root)
                stage = "launch"
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir), headless=False,
                    executable_path=config["executable"],
                    args=["--no-sandbox", "--disable-dev-shm-usage"])
                page = context.pages[0] if context.pages else context.new_page()
                stage = "navigate"
                _open_conversation(page, config["url"])
                print("[messages-watcher] monitoring fixed conversation",
                      flush=True)
                stage = "baseline"
                if not initialized:
                    # Snapshot any old visible trigger before monitoring begins;
                    # startup must never act on conversation history.
                    baseline = _latest_trigger(page)
                    if baseline:
                        receipts.add(trigger_fingerprint(baseline))
                    _write_receipts(Path(profile_dir), receipts)
                    initialized = True
                stage = "poll"
                while True:
                    page.wait_for_timeout(int(POLL_SECONDS * 1000))
                    if "/welcome" in page.url:
                        raise RuntimeError("Google Messages pairing is not active")
                    identity = _latest_trigger(page)
                    if not identity:
                        continue
                    fingerprint = trigger_fingerprint(identity)
                    if fingerprint in receipts:
                        continue
                    context.close(); context = None
                    lock.release(); lock = None
                    if _submit(config, fingerprint):
                        receipts.add(fingerprint)
                        _write_receipts(Path(profile_dir), receipts)
                        # Keep the profile free until the queued Cloud worker
                        # has had ample time to run the existing sender.
                        time.sleep(SEND_WINDOW_SECONDS)
                    else:
                        time.sleep(10)
                    break
            except Exception as exc:
                print(f"[messages-watcher] unavailable at {stage}: "
                      f"{type(exc).__name__}",
                      flush=True)
                time.sleep(10)
            finally:
                if context is not None:
                    try: context.close()
                    except Exception: pass
                if lock is not None:
                    try: lock.release()
                    except Exception: pass


if __name__ == "__main__":
    run()
