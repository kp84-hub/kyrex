#!/usr/bin/env python3
"""
check_setup.py — Kyrex Cloud Agent, self-hosting setup validator.

Run this BEFORE deploying to catch the same setup mistakes hit while
building this the first time: an env var that's empty, a token that's just
wrong, a bot token that got revoked, a malformed export. Checks each
credential live against its real API instead of only checking that the
env var is non-empty — an env var can be "set" and still be worthless.

Usage:
    python3 check_setup.py
"""
import json
import os
import sys
import urllib.request
import urllib.error

TELEGRAM_API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")


def check(name, ok, detail=""):
    mark = "✅" if ok else "❌"
    print(f"{mark} {name}{': ' + detail if detail else ''}")
    return ok


def check_telegram_token():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return check("TELEGRAM_BOT_TOKEN", False, "not set")
    try:
        with urllib.request.urlopen(f"{TELEGRAM_API_BASE}/bot{token}/getMe", timeout=15) as resp:
            data = json.loads(resp.read())
        if data.get("ok"):
            return check("TELEGRAM_BOT_TOKEN", True, f"bot @{data['result']['username']}")
        return check("TELEGRAM_BOT_TOKEN", False, "token rejected by Telegram")
    except urllib.error.HTTPError as e:
        return check("TELEGRAM_BOT_TOKEN", False, f"HTTP {e.code} — revoked or mistyped token")
    except Exception as e:
        return check("TELEGRAM_BOT_TOKEN", False, f"{type(e).__name__}: {e}")


def check_chat_id():
    val = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID")
    if not val:
        return check("TELEGRAM_ALLOWED_CHAT_ID", False, "not set")
    try:
        int(val)
    except ValueError:
        return check("TELEGRAM_ALLOWED_CHAT_ID", False, f"'{val}' is not a valid integer")
    return check("TELEGRAM_ALLOWED_CHAT_ID", True, val)


def check_github_token():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return check("GITHUB_TOKEN", False, "not set")
    req = urllib.request.Request("https://api.github.com/user",
                                  headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return check("GITHUB_TOKEN", True, f"authenticated as {data.get('login')}")
    except urllib.error.HTTPError as e:
        return check("GITHUB_TOKEN", False, f"HTTP {e.code} — missing, wrong, or lacks 'repo' scope")
    except Exception as e:
        return check("GITHUB_TOKEN", False, f"{type(e).__name__}: {e}")


def check_llm_config():
    provider = os.environ.get("KYREX_PROVIDER", "openai")
    api_key = os.environ.get("KYREX_API_KEY")
    model = os.environ.get("KYREX_MODEL")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

    if not api_key:
        return check("KYREX_API_KEY", False, "not set")
    if not model:
        return check("KYREX_MODEL", False, "not set")

    payload = json.dumps({"model": model, "max_tokens": 5,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
    if provider == "anthropic":
        endpoint = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com") + "/v1/messages"
        req = urllib.request.Request(endpoint, data=payload, method="POST",
                                      headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                               "content-type": "application/json"})
    else:
        endpoint = f"{base_url.rstrip('/')}/chat/completions"
        req = urllib.request.Request(endpoint, data=payload, method="POST",
                                      headers={"Authorization": f"Bearer {api_key}",
                                               "content-type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if "error" in data:
            return check(f"KYREX_API_KEY ({provider}/{model})", False, data["error"].get("message", str(data["error"])))
        return check(f"KYREX_API_KEY ({provider}/{model})", True, "model responded")
    except urllib.error.HTTPError as e:
        return check(f"KYREX_API_KEY ({provider}/{model})", False, f"HTTP {e.code}")
    except Exception as e:
        return check(f"KYREX_API_KEY ({provider}/{model})", False, f"{type(e).__name__}: {e}")


def main():
    print("=== Kyrex Cloud Setup Check ===\n")
    results = [
        check_telegram_token(),
        check_chat_id(),
        check_github_token(),
        check_llm_config(),
    ]
    print()
    if all(results):
        print("All checks passed — setup looks good.")
        return 0
    print("Some checks failed. Fix the issues above before deploying.")
    return 1


if __name__ == "__main__":
    sys.exit(main())