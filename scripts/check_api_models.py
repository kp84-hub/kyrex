#!/usr/bin/env python3
"""
Test every model connected to a Kyrex / OpenAI-compatible API key.

1. Enumerates models from {base_url}/models  (this gateway exposes the list
   publicly, so it works even before auth).
2. Sends one tiny chat completion to EACH model and records pass/fail,
   latency, and any error.

Self-contained: only uses the stdlib (urllib) so it runs anywhere Python 3
is available with network egress to the endpoint.

The key + base_url are auto-loaded from ~/.px/config.json (same source the
Kyrex TUI/engine use). Override on the CLI if you want to target a different
key/endpoint (e.g. Together.ai, OpenRouter, OpenAI, ...).

Usage:
    python3 test_api_key_models.py
    python3 test_api_key_models.py --only hy3,glm-5,deepseek-v4-flash
    python3 test_api_key_models.py --base-url https://api.together.xyz/v1 \
        --api-key sk-...
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def _headers(api_key):
    h = {"Content-Type": "application/json"}
    if api_key:
        h["Authorization"] = "Bearer " + api_key
    return h


def load_config_defaults():
    """Mirror tui/model.go loadPXConfig(): read ~/.px/config.json."""
    base_url = api_key = None
    path = os.path.expanduser("~/.px/config.json")
    try:
        with open(path) as f:
            c = json.load(f)
        base_url = c.get("base_url") or None
        if c.get("api_key"):
            api_key = c["api_key"]
        elif c.get("api_key_env"):
            api_key = os.environ.get(c["api_key_env"]) or None
    except Exception:
        pass
    return base_url, api_key


def list_models(base_url, api_key, timeout):
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers=_headers(api_key))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    items = data.get("data") or data.get("models") or []
    return [m["id"] for m in items if isinstance(m, dict) and m.get("id")]


def ping_model(base_url, api_key, model, max_tokens, timeout):
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": "Reply with exactly one word: pong"}
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers=_headers(api_key), method="POST"
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.load(r)
        dt = time.time() - t0
        content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {
            "ok": True,
            "model": model,
            "latency_s": round(dt, 3),
            "reply": content.strip()[:120],
            "error": None,
        }
    except urllib.error.HTTPError as e:
        dt = time.time() - t0
        body = e.read().decode(errors="replace")[:300]
        return {
            "ok": False,
            "model": model,
            "latency_s": round(dt, 3),
            "reply": None,
            "error": f"HTTP {e.code}: {body}",
        }
    except Exception as e:  # DNS / timeout / connection errors
        dt = time.time() - t0
        return {
            "ok": False,
            "model": model,
            "latency_s": round(dt, 3),
            "reply": None,
            "error": f"{type(e).__name__}: {e}",
        }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default=None, help="API base URL")
    ap.add_argument("--api-key", default=None, help="API key (Bearer)")
    ap.add_argument(
        "--only",
        default=None,
        help="comma-separated model ids to test (default: all enumerated)",
    )
    ap.add_argument("--max-tokens", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument("--out", default="model_test_report.json")
    args = ap.parse_args()

    cfg_base, cfg_key = load_config_defaults()
    base_url = args.base_url or cfg_base or "https://opencode.ai/zen/go/v1"
    api_key = args.api_key or cfg_key or ""

    print(f"[*] base_url : {base_url}")
    print(f"[*] api_key  : {'present' if api_key else 'absent (list-only auth)'}")
    print(f"[*] Enumerating models from {base_url}/models ...")

    try:
        models = list_models(base_url, api_key, args.timeout)
    except Exception as e:
        print(
            f"[!] Failed to enumerate models: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"[*] Found {len(models)} models.")
    if args.only:
        wanted = {m.strip() for m in args.only.split(",") if m.strip()}
        models = [m for m in models if m in wanted]
        print(f"[*] Filtered to {len(models)} models.")

    results = []
    for i, m in enumerate(models, 1):
        print(f"[{i}/{len(models)}] testing {m} ...", end=" ", flush=True)
        res = ping_model(base_url, api_key, m, args.max_tokens, args.timeout)
        if res["ok"]:
            print(f"OK  {res['latency_s']}s  reply={res['reply']!r}")
        else:
            print(f"FAIL {res['latency_s']}s  {res['error']}")
        results.append(res)

    passed = sum(1 for r in results if r["ok"])
    print(f"\n=== SUMMARY: {passed}/{len(results)} models responded OK ===")

    report = {
        "base_url": base_url,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
    }
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"[*] Report written to {args.out}")


if __name__ == "__main__":
    main()
