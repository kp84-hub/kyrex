"""Tests for per-Bot LLM configuration — saved provider profiles.

Two Bots owned by the same user run on DIFFERENT profiles and models.
Proves, per request, that each turn hits its own base URL with its own
model and API key, that hop-by-hop headers from a profile are stripped,
that secrets never appear in any public surface (profile reads, bot
registry, disk store), that an unconfigured Bot fails clearly with no
global-env fallback (unless the documented migration flag is set), and
that the executor child receives the profile via its environment.

Run: python3 test_bot_provider_profiles.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="kyrex_botprof_")
os.environ["KYREX_DATA_DIR"] = _TMP
os.environ["KYREX_PROFILE_SECRET"] = "test-profile-secret"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "1")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bots
import intent
import profiles
import serve

failures = []
KEY_OR = "sk-OR-SECRET-1111"
KEY_OAI = "sk-OAI-SECRET-2222"
KEY_GLOBAL = "sk-GLOBAL-do-not-use"


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# ── Fixtures: two profiles, same owner; two bots, different configs ───

PROFILE_A = profiles.create_profile(
    owner="alice", name="OpenRouter Dev", provider="openrouter",
    base_url="https://openrouter.example/api/v1",
    api_key=KEY_OR,
    models=["deepseek/deepseek-v4.1-flash", "other/model"],
    custom_headers={"X-Team": "devs", "Authorization": "EVIL-VALUE"},
)
PROFILE_B = profiles.create_profile(
    owner="alice", name="House OpenAI", provider="openai",
    base_url="https://api.openai.example/v1",
    api_key=KEY_OAI,
    models=["gpt-4o-mini"],
)
bots.add_bot("dev", "Dev Bot", "deepseek/deepseek-v4.1-flash",
             str(Path(_TMP) / "rifts" / "dev"),
             owner="",  # legacy operator-owned bot
             provider_profile_id=PROFILE_A["id"])
bots.add_bot("writer", "Writer Bot", "gpt-4o-mini",
             str(Path(_TMP) / "rifts" / "writer"),
             owner="alice",
             provider_profile_id=PROFILE_B["id"])
bots.add_bot("noconf", "Unconfigured Bot", "some/model",
             str(Path(_TMP) / "rifts" / "noconf"),
             owner="alice")

print("\nTest 1: two bots resolve to different profiles/models — independent")
cfg_dev = profiles.resolve_for_bot(bots.get_bot("dev"))
cfg_writer = profiles.resolve_for_bot(bots.get_bot("writer"))
check("dev bot → OpenRouter base URL",
      cfg_dev.base_url == "https://openrouter.example/api/v1", cfg_dev.base_url)
check("dev bot → exact deepseek model",
      cfg_dev.model == "deepseek/deepseek-v4.1-flash", cfg_dev.model)
check("dev bot → its own key", cfg_dev.api_key == KEY_OR, cfg_dev.api_key)
check("writer bot → House OpenAI base URL",
      cfg_writer.base_url == "https://api.openai.example/v1", cfg_writer.base_url)
check("writer bot → its exact model", cfg_writer.model == "gpt-4o-mini",
      cfg_writer.model)
check("writer bot → its own key", cfg_writer.api_key == KEY_OAI, cfg_writer.api_key)
check("profiles do not cross-contaminate",
      cfg_dev.base_url != cfg_writer.base_url and cfg_dev.api_key != cfg_writer.api_key)

print("\nTest 1b: env_overrides route the child to the profile")
ov = cfg_dev.env_overrides()
check("child provider is openai-compatible", ov["KYREX_PROVIDER"] == "openai", ov)
check("child base URL override", ov["OPENAI_BASE_URL"] == "https://openrouter.example/api/v1", ov)
check("child model override", ov["KYREX_MODEL"] == "deepseek/deepseek-v4.1-flash", ov)
check("child key override present", ov["KYREX_API_KEY"] == KEY_OR)
check("approved custom headers forwarded",
      json.loads(ov["KYREX_CUSTOM_HEADERS"]).get("X-Team") == "devs",
      ov.get("KYREX_CUSTOM_HEADERS"))
check("hop-by-hop header stripped from profile",
      "Authorization" not in json.loads(ov["KYREX_CUSTOM_HEADERS"]),
      ov.get("KYREX_CUSTOM_HEADERS"))
check("config headers never contain auth", cfg_dev.headers.get("Authorization") is None,
      cfg_dev.headers)

# ── Per-request proof via mocked HTTP ─────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return self._payload


_CAPTURED = []


def _capture_urlopen(req, timeout=None, **kw):
    headers = {k.lower(): v for k, v in req.headers.items()}
    _CAPTURED.append({
        "url": req.full_url,
        "headers": headers,
        "body": json.loads(req.data.decode()),
    })
    return _FakeResp({"choices": [{"message": {"content": "hello from the model"}}]})


print("\nTest 2: each chat request hits its OWN base URL, model, and key")
with patch("urllib.request.urlopen", _capture_urlopen):
    answer_a = intent.answer_chat("hi", history=[], provider_config=cfg_dev)
    answer_b = intent.answer_chat("hi", history=[], provider_config=cfg_writer)
req_a, req_b = _CAPTURED[0], _CAPTURED[1]
check("dev request → OpenRouter endpoint",
      req_a["url"] == "https://openrouter.example/api/v1/chat/completions", req_a["url"])
check("dev request body model is deepseek",
      req_a["body"]["model"] == "deepseek/deepseek-v4.1-flash", req_a["body"]["model"])
check("dev request carries its own key",
      req_a["headers"].get("authorization") == f"Bearer {KEY_OR}",
      req_a["headers"].get("authorization"))
check("dev request carries approved header",
      req_a["headers"].get("x-team") == "devs", req_a["headers"].get("x-team"))
check("writer request → House OpenAI endpoint",
      req_b["url"] == "https://api.openai.example/v1/chat/completions", req_b["url"])
check("writer request body model is gpt-4o-mini",
      req_b["body"]["model"] == "gpt-4o-mini", req_b["body"]["model"])
check("writer request carries its own key",
      req_b["headers"].get("authorization") == f"Bearer {KEY_OAI}",
      req_b["headers"].get("authorization"))
check("chat answers are the model text (no key echo)",
      answer_a == "hello from the model" and answer_b == "hello from the model",
      (answer_a, answer_b))

print("\nTest 3: classification runs on the Bot's profile too")
_CAPTURED.clear()
with patch("urllib.request.urlopen", _capture_urlopen):
    verdict = intent.classify_intent("delete the temp dir",
                                     provider_config=cfg_writer)
req_c = _CAPTURED[0]
check("classify request → writer bot endpoint",
      req_c["url"] == "https://api.openai.example/v1/chat/completions", req_c["url"])
check("classify request model is the writer bot's",
      req_c["body"]["model"] == "gpt-4o-mini", req_c["body"]["model"])

print("\nTest 4: custom headers never override auth")
evil_cfg = profiles.ProviderConfig(
    provider="openai", base_url="https://api.openai.example/v1",
    api_key=KEY_OAI, model="gpt-4o-mini",
    headers={"Authorization": "Bearer EVIL", "x-api-key": "EVIL",
             "Cookie": "session=evil", "X-Legit": "yes"})
_CAPTURED.clear()
with patch("urllib.request.urlopen", _capture_urlopen):
    intent.answer_chat("hi", provider_config=evil_cfg)
evil_req = _CAPTURED[0]
check("Authorization not overridable",
      evil_req["headers"].get("authorization") == f"Bearer {KEY_OAI}",
      evil_req["headers"].get("authorization"))
check("Cookie stripped", "cookie" not in evil_req["headers"],
      evil_req["headers"].keys())
check("legit custom header passes", evil_req["headers"].get("x-legit") == "yes")

# ── Secret hygiene ─────────────────────────────────────────────────────

print("\nTest 5: secrets never appear in public surfaces")
public_blob = json.dumps([
    profiles.list_profiles("alice"),
    profiles.get_profile(PROFILE_A["id"]),
    bots.list_bots(),
])
check("no key in profile reads / bot registry", KEY_OR not in public_blob
      and KEY_OAI not in public_blob)
check("last4 shown for display",
      PROFILE_A["api_key_last4"] == "1111" and PROFILE_B["api_key_last4"] == "2222",
      (PROFILE_A["api_key_last4"], PROFILE_B["api_key_last4"]))
check("no ciphertext in public reads", "api_key_encrypted" not in public_blob)
on_disk = Path(profiles.PROFILES_FILE).read_text()
check("keys encrypted at rest",
      KEY_OR not in on_disk and KEY_OAI not in on_disk)
check("ciphertext at rest", "api_key_encrypted" in on_disk)

# ── Fail-closed behaviour ──────────────────────────────────────────────

print("\nTest 6: unconfigured bot fails clearly — no global-env fallback")
os.environ["KYREX_API_KEY"] = KEY_GLOBAL
os.environ["KYREX_MODEL"] = "global/model"
os.environ["OPENAI_BASE_URL"] = "https://global.example/v1"
try:
    saved_flag = os.environ.pop("KYREX_BOT_MIGRATE_GLOBAL_FALLBACK", None)
    try:
        profiles.resolve_for_bot(bots.get_bot("noconf"))
        check("resolution refuses unconfigured bot", False, "no exception")
    except profiles.ProfileResolutionError as exc:
        check("resolution refuses unconfigured bot",
              "no provider profile configured" in str(exc), str(exc))
    ctx = serve.build_context("noconf")
    check("context carries the clear error",
          ctx.provider is None and ctx.provider_error is not None
          and "no provider profile configured" in ctx.provider_error,
          ctx.provider_error)
finally:
    if saved_flag is not None:
        os.environ["KYREX_BOT_MIGRATE_GLOBAL_FALLBACK"] = saved_flag

print("\nTest 7: documented migration — flag set → global env, stamped once")
try:
    os.environ["KYREX_BOT_MIGRATE_GLOBAL_FALLBACK"] = "1"
    ctx = serve.build_context("noconf")
    check("migration: no provider override, no error",
          ctx.provider is None and ctx.provider_error is None,
          (ctx.provider, ctx.provider_error))
    check("migration stamped legacy_global",
          bots.get_bot("noconf").get("legacy_global") is True)
finally:
    os.environ.pop("KYREX_BOT_MIGRATE_GLOBAL_FALLBACK", None)

print("\nTest 8: broken profile reference fails clearly")
bots.update_bot("writer", provider_profile_id="prf_doesnotexist")
try:
    profiles.resolve_for_bot(bots.get_bot("writer"))
    check("missing profile refused", False, "no exception")
except profiles.ProfileResolutionError as exc:
    check("missing profile refused", "does not exist" in str(exc), str(exc))
bots.update_bot("writer", provider_profile_id=PROFILE_B["id"])

print("\nTest 9: model must belong to the profile")
bots.update_bot("dev", model="gpt-4o-mini")  # not in PROFILE_A's models
try:
    profiles.resolve_for_bot(bots.get_bot("dev"))
    check("unapproved model refused", False, "no exception")
except profiles.ProfileResolutionError as exc:
    check("unapproved model refused", "not approved for profile" in str(exc), str(exc))
bots.update_bot("dev", model="deepseek/deepseek-v4.1-flash")

print("\nTest 10: owner mismatch refused; operator rule documented")
bots.add_bot("bobbot", "Bob Bot", "deepseek/deepseek-v4.1-flash",
             str(Path(_TMP) / "rifts" / "bobbot"),
             owner="bob", provider_profile_id=PROFILE_A["id"])
try:
    profiles.resolve_for_bot(bots.get_bot("bobbot"))
    check("foreign owner refused", False, "no exception")
except profiles.ProfileResolutionError as exc:
    check("foreign owner refused", "not owned by bot" in str(exc), str(exc))
try:
    profiles.validate_bot_assignment(PROFILE_A["id"], "bob",
                                     "deepseek/deepseek-v4.1-flash")
    check("assignment validation refuses foreign owner", False, "no exception")
except profiles.ProfileError as exc:
    check("assignment validation refuses foreign owner",
          "not owned by" in str(exc), str(exc))

print("\nTest 11: bot registry API — profile/model fields, keys never accepted")
check("update_bot accepts provider_profile_id",
      bots.update_bot("writer", provider_profile_id=PROFILE_A["id"])["provider_profile_id"]
      == PROFILE_A["id"])
try:
    bots.update_bot("writer", api_key="sk-EVIL")
    check("registry rejects key material", False, "no exception")
except ValueError:
    check("registry rejects key material", True)
bots.update_bot("writer", provider_profile_id=PROFILE_B["id"])  # restore

# ── Executor child receives the profile env ───────────────────────────

print("\nTest 12: end-to-end — executor child runs on the Bot's provider")
FAKE_ENV_EXECUTOR = Path(_TMP) / "fake_env_executor.py"
FAKE_ENV_EXECUTOR.write_text(
    "import json, os, sys\n"
    "env = {k: os.environ.get(k, '') for k in "
    "('KYREX_PROVIDER', 'KYREX_MODEL', 'OPENAI_BASE_URL', 'KYREX_CUSTOM_HEADERS')}\n"
    "print('KYREX_RESULT_JSON:' + json.dumps({'status': 'ok', 'env': env}))\n"
)
serve.EXECUTORS["fakeprof"] = str(FAKE_ENV_EXECUTOR)

captured = {}
result_box = {}

def fake_send(chat_id, text):
    captured.setdefault("sends", []).append(text)
    return f"msg-{len(captured['sends'])}"

os.environ["OPENAI_BASE_URL"] = "https://global.example/v1"
os.environ["KYREX_MODEL"] = "global/model"
try:
    serve.run_task(
        chat_id="chat-1", repo_url=None, task_text="say hi",
        executor_prefix="fakeprof", session_key="dev",
        send=fake_send, edit=lambda c, m, t: None,
        on_result=lambda r: result_box.update(r or {}),
    )
finally:
    pass
child_env = (result_box.get("env") or {})
check("child saw the profile base URL (not the global one)",
      child_env.get("OPENAI_BASE_URL") == "https://openrouter.example/api/v1",
      child_env.get("OPENAI_BASE_URL"))
check("child saw the bot's exact model (not the global one)",
      child_env.get("KYREX_MODEL") == "deepseek/deepseek-v4.1-flash",
      child_env.get("KYREX_MODEL"))
check("child custom headers from the profile",
      json.loads(child_env.get("KYREX_CUSTOM_HEADERS") or "{}").get("X-Team") == "devs")
blob = json.dumps(result_box) + json.dumps(captured)
check("no key material leaked into result/messages", KEY_OR not in blob
      and KEY_GLOBAL not in blob)

print("\nTest 13: LLM executor for an unconfigured bot fails closed")
FAKE_REPO_EXECUTOR = Path(_TMP) / "fake_repo_executor.py"
FAKE_REPO_EXECUTOR.write_text(
    "import json, sys\n"
    "print('KYREX_RESULT_JSON:' + json.dumps({'status': 'ok'}))\n"
)
old_repo = serve.EXECUTORS.get("repo")
serve.EXECUTORS["repo"] = str(FAKE_REPO_EXECUTOR)
sends_before = len(captured.get("sends", []))
raised = None
try:
    serve.run_task(
        chat_id="chat-1", repo_url=None, task_text="x",
        executor_prefix="repo", session_key="noconf",
        send=fake_send, edit=lambda c, m, t: None,
        on_result=lambda r: None,
    )
except RuntimeError as exc:
    raised = exc
finally:
    if old_repo is not None:
        serve.EXECUTORS["repo"] = old_repo
# run_task may raise OR deliver the failure to the transport as a ⚠️ send —
# both are fail-closed; what must never happen is the executor running.
new_sends = captured.get("sends", [])[sends_before:]
surfaced = (raised is not None and "no provider profile configured" in str(raised)) or \
    any("no provider profile configured" in s for s in new_sends)
check("unconfigured bot LLM task refused",
      surfaced,
      f"raised={raised!r} sends={new_sends!r}")
check("fail-closed error reached the transport",
      any("⚠️" in s and "no provider profile configured" in s for s in new_sends)
      or (raised is not None),
      f"sends={new_sends!r}")

print("\nTest 14: regular chat path unchanged (env-configured)")
reg_cfg_none = intent._resolve_provider(None)
check("no provider_config → env provider values",
      reg_cfg_none[1] == "global/model" and reg_cfg_none[2] == KEY_GLOBAL
      and reg_cfg_none[3] == "https://global.example/v1",
      reg_cfg_none[:4])

# ── Transport/API contract strings ─────────────────────────────────────

print("\nTest 15: composer/API contract (string checks, test_flux_frontend style)")
_CLOUD_DIR = Path(__file__).resolve().parent
tg_src = (_CLOUD_DIR / "telegram_bot.py").read_text()
check("bot chat turn uses the profile",
      "answer_chat(text_for_task, history=_hist, provider_config=bot_cfg)" in tg_src)
check("regular chat path unchanged (env)",
      "answer_chat(text_for_task, history=_hist)\n" in tg_src)
check("bot turns fail closed without a profile",
      "send_message(chat_id, \"⚠️ \" + bot_err)" in tg_src)
check("/setbot supports profile field", '"profile": "provider_profile_id"' in tg_src)
check("/bots displays profile read-only",
      "profile: {profile}" in tg_src or "· profile: {profile}" in tg_src)
web_src = (_CLOUD_DIR / "web" / "backend" / "main.py").read_text()
for route in ("/api/profiles", '"/api/profiles/{profile_id}"', "/api/bots",
              '"/api/bots/{bot_id}"'):
    check(f"web API exposes {route}", route.split('"')[0] in web_src or route in web_src)
check("web create accepts write-only api_key", 'body.get("api_key")' in web_src)
check("web responses expose last4, never the key",
      "api_key_last4" in web_src and "api_key_encrypted" not in web_src)

# ── Summary ───────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
