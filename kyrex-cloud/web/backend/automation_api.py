"""Disabled-by-default, owner-bound gateway for the outbound VPS email watcher.

Only exact-sender Gmail reads can be queued. Destinations come exclusively from
server configuration, never from a client event or email text. The existing
worker and task-to-conversation recovery remain the delivery path.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from email.utils import getaddresses

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

import bots
import chat_service
import connectors
import dev_bot
import automation_rules
from task_store import DuplicateTaskId

router = APIRouter(prefix="/api/automations/email", tags=["automations"])
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SENDER = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z")


def _settings(request):
    if os.environ.get("KYREX_AUTOMATION_ENABLED") != "1":
        raise HTTPException(404, "Automation is disabled")
    token = os.environ.get("KYREX_AUTOMATION_TOKEN", "")
    owner = os.environ.get("KYREX_AUTOMATION_OWNER", "")
    if (len(token) < 32 or any(c.isspace() for c in token)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner)):
        raise HTTPException(503, "Automation credentials are not configured")
    provided = request.headers.get("authorization", "")
    if not hmac.compare_digest(provided.encode(), ("Bearer " + token).encode()):
        raise HTTPException(401, "Invalid automation credential")
    try:
        raw = automation_rules.list_rules(owner)
        if len(raw) > 20:
            raise ValueError()
        rules = {}
        for item in raw:
            if (not _ID.fullmatch(item["rule_id"])
                    or not _ID.fullmatch(item["bot_id"])
                    or not _SENDER.fullmatch(item["sender"])
                    or not re.fullmatch(r"[a-f0-9]{32}", item["conversation_id"])
                    or item["rule_id"] in rules):
                raise ValueError()
            rule = {
                "id": item["rule_id"], "enabled": bool(item["enabled"]),
                "sender": item["sender"].lower(), "bot_id": item["bot_id"],
                "conversation_id": item["conversation_id"],
            }
            rule["version"] = hashlib.sha256(json.dumps(
                [owner, rule["id"], rule["sender"], rule["bot_id"],
                 rule["conversation_id"]], separators=(",", ":")).encode()).hexdigest()
            rules[rule["id"]] = rule
    except (ValueError, TypeError, KeyError):
        raise HTTPException(503, "Automation rules are invalid") from None
    return owner, rules


def _rule(request, rule_id, version):
    owner, rules = _settings(request)
    rule = rules.get(rule_id)
    if not rule or not rule["enabled"]:
        raise HTTPException(404, "Automation rule is unavailable")
    if version != rule["version"]:
        raise HTTPException(409, "Automation rule changed; reload rules")
    bot = bots.load_bots().get(rule["bot_id"])
    if not bot or bot.get("owner") != owner or not bots.is_running(bot):
        raise HTTPException(409, "Destination bot is unavailable")
    # Do not mutate the transcript from this background gateway. Delivery is
    # recovered by the existing Chat path from the durable task record.
    try:
        conv = json.loads(chat_service._conv_path(
            owner, rule["conversation_id"]).read_text())
    except (OSError, ValueError):
        raise HTTPException(409, "Destination conversation is unavailable") from None
    if (not isinstance(conv, dict)
            or conv.get("conversation_id") != rule["conversation_id"]
            or conv.get("bot_id") != rule["bot_id"]):
        raise HTTPException(409, "Destination conversation does not match the bot")
    if not connectors.default_store().gmail_read_available(owner):
        raise HTTPException(409, "Gmail read access is unavailable")
    return owner, rule, bot


def _require_owner(request: Request) -> str:
    import main
    user = main.require_user(request)
    configured = os.environ.get("KYREX_AUTOMATION_OWNER", "").strip()
    if configured and user != configured:
        raise HTTPException(403, "Email automation is not enabled for this account")
    return user


@router.get("/managed")
def managed_rules(request: Request):
    owner = _require_owner(request)
    output = []
    chats = chat_service.list_conversations(owner)
    by_id = {c.get("conversation_id"): c for c in chats}
    visible_bots = {b["id"]: b for b in chat_service.list_bots_for_user(owner)}
    for rule in automation_rules.list_rules(owner):
        bot = visible_bots.get(rule["bot_id"], {})
        chat = by_id.get(rule["conversation_id"], {})
        output.append({**rule, "bot_name": bot.get("name") or "Unavailable bot",
                       "conversation_title": chat.get("title") or "Unavailable conversation"})
    configured_owner = os.environ.get("KYREX_AUTOMATION_OWNER", "").strip()
    token = os.environ.get("KYREX_AUTOMATION_TOKEN", "")
    service_ready = (os.environ.get("KYREX_AUTOMATION_ENABLED") == "1"
                     and configured_owner == owner and len(token) >= 32
                     and not any(c.isspace() for c in token))
    return {"rules": output, "service_ready": service_ready}


class ManagedRuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender: str = Field(min_length=3, max_length=254)
    bot_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    conversation_id: str = Field(pattern=r"^[a-f0-9]{32}$")


@router.post("/managed")
def create_managed_rule(request: Request, body: ManagedRuleInput):
    owner = _require_owner(request)
    sender = body.sender.strip().lower()
    if not _SENDER.fullmatch(sender):
        raise HTTPException(422, "Enter one sender email address")
    bot = bots.load_bots().get(body.bot_id)
    if not bot or bot.get("owner") != owner or not bots.is_running(bot):
        raise HTTPException(409, "Choose a running bot owned by this account")
    try:
        conv = json.loads(chat_service._conv_path(owner, body.conversation_id).read_text())
    except (OSError, ValueError):
        raise HTTPException(409, "Choose an existing bot conversation") from None
    if (not isinstance(conv, dict) or conv.get("conversation_id") != body.conversation_id
            or conv.get("bot_id") != body.bot_id):
        raise HTTPException(409, "Conversation must belong to the selected bot")
    if not connectors.default_store().gmail_read_available(owner):
        raise HTTPException(409, "Connect Gmail read access before enabling email rules")
    try:
        rule = automation_rules.create_rule(owner, sender, body.bot_id, body.conversation_id)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "This sender already has a rule for that conversation") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return rule


class ManagedRuleState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


@router.patch("/managed/{rule_id}")
def update_managed_rule(request: Request, rule_id: str, body: ManagedRuleState):
    owner = _require_owner(request)
    if not _ID.fullmatch(rule_id):
        raise HTTPException(404, "Rule not found")
    if not automation_rules.set_enabled(owner, rule_id, body.enabled):
        raise HTTPException(404, "Rule not found")
    return {"updated": True}


@router.delete("/managed/{rule_id}")
def remove_managed_rule(request: Request, rule_id: str):
    owner = _require_owner(request)
    if not _ID.fullmatch(rule_id) or not automation_rules.delete_rule(owner, rule_id):
        raise HTTPException(404, "Rule not found")
    return {"deleted": True}


@router.get("/rules")
def list_rules(request: Request):
    _, rules = _settings(request)
    return {"rules": [{"id": r["id"], "version": r["version"]}
                      for r in rules.values() if r["enabled"]]}


@router.get("/{rule_id}/candidates")
def candidates(request: Request, rule_id: str, version: str,
               page_token: str = ""):
    owner, rule, _ = _rule(request, rule_id, version)
    if len(page_token) > 4096 or any(c.isspace() for c in page_token):
        raise HTTPException(400, "Invalid page token")
    try:
        page = connectors.default_store().gmail(owner).search(
            query=f'in:inbox from:{rule["sender"]} newer_than:7d',
            max_results=50, page_token=page_token or None)
        ids = [m["id"] for m in page["messages"]]
        if any(not isinstance(mid, str) or not _ID.fullmatch(mid) for mid in ids):
            raise ValueError()
    except Exception:
        raise HTTPException(502, "Gmail candidate lookup failed") from None
    return {"message_ids": ids, "next_page_token": page["next_page_token"]}


class EmailEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(pattern=r"^[a-f0-9]{64}$")
    message_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


@router.post("/{rule_id}/events")
def submit_event(request: Request, rule_id: str, event: EmailEvent):
    owner, rule, bot = _rule(request, rule_id, event.version)
    # No caller-supplied task, owner, destination, provider, or approval value.
    task_id = "auto-email-" + hashlib.sha256(json.dumps(
        [owner, rule_id, event.message_id], separators=(",", ":")).encode()).hexdigest()
    store = chat_service._task_store()
    existing = store.get(task_id)
    if existing:
        return _receipt(existing, owner, rule, task_id, duplicate=True)
    try:
        metadata = connectors.default_store().gmail(owner).message(event.message_id)
    except Exception:
        raise HTTPException(502, "Gmail message lookup failed") from None
    addresses = getaddresses([str(metadata.get("headers", {}).get("From") or "")])
    if (metadata.get("owner") != owner or metadata.get("id") != event.message_id
            or len(addresses) != 1 or addresses[0][1].lower() != rule["sender"]):
        raise HTTPException(409, "Message does not match the configured sender")
    try:
        dev_bot.submit_gmail_task(
            owner, bot, f"gmail: read id {event.message_id}", store=store,
            conversation_id=rule["conversation_id"], task_id=task_id)
    except (DuplicateTaskId, sqlite3.IntegrityError):
        # Another request/process may have won the unique task-id insert.
        existing = store.get(task_id)
        if not existing:
            raise
        return _receipt(existing, owner, rule, task_id, duplicate=True)
    except dev_bot.DevBotError:
        raise HTTPException(409, "Email task could not be queued") from None
    return {"accepted": True, "duplicate": False, "task_id": task_id}


def _receipt(task, owner, rule, task_id, *, duplicate):
    if (task.get("chat_id") != owner or task.get("bot_id") != rule["bot_id"]
            or task.get("conversation_id") != rule["conversation_id"]
            or task.get("executor_prefix") != "gmail"):
        # Repointing a rule must never replay an old message into a new chat.
        raise HTTPException(409, "Event already belongs to another destination")
    return {"accepted": True, "duplicate": duplicate, "task_id": task_id}
