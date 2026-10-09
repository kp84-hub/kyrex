"""Hourly Glofox monitoring. No Facebook, LLM, or automatic uncertain resend."""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime

import glofox_api
from level6_trainer_store import EASTERN, TrainerStore


def enabled():
    return os.environ.get('KYREX_LEVEL6_TRAINER_MONITOR_ENABLED') == '1'


def calendar_context(owner, bot_id):
    import bots
    import serve
    bot = bots.get_bot(bot_id)
    if (not bot or bot.get('owner') != owner or not bots.is_running(bot)
            or not serve.is_calendar_bot_policy(bot.get('policy'))):
        raise ValueError('Choose a running Calendar Bot owned by this account')
    ctx = serve.build_context(bot_id, 'calendar')
    if ctx.bot_owner != owner or not serve.is_calendar_bot_policy(ctx.policy):
        raise ValueError('Calendar Bot grant is unavailable')
    return ctx


def group_ready(owner, bot_id):
    import browser_hosts
    if os.environ.get('KYREX_LEVEL6_SEND_ENABLED') != '1':
        return 'Group delivery is disabled on the server'
    host = browser_hosts.host_for(owner, bot_id)
    if not host or not host.is_available():
        return 'Connect the Calendar Bot’s Browser Host for group delivery'
    return ''


def send_group(cfg, attempt, ctx):
    import serve
    spec = json.dumps({'google_messages_level6': True,
        'url': 'https://messages.google.com/web/', 'message': attempt['message'],
        'trainer_change': {'date': attempt['day'], 'version': attempt['version'],
            'attempt': attempt['attempt'], 'owner_key': hashlib.sha256(cfg['owner'].encode()).hexdigest()[:16]}},
        ensure_ascii=False)
    result, error = serve.browser_host_dispatch(ctx, spec,
        task_id=f"l6-trainer-{attempt['id']}-{attempt['attempt']}",
        profile_bot_id=serve.LEVEL6_MESSAGES_PROFILE_ID)
    if error or not isinstance(result, dict):
        # Host disconnection/timeout cannot prove that no click occurred.
        return 'unknown', 'Host delivery outcome is unknown; check the group before manually resending'
    if result.get('status') in ('ok', 'no_changes'):
        return 'sent', 'Delivered to the configured L6 group'
    if result.get('delivery_state') == 'failed':
        return 'failed', 'Host could not send; verify its pinned conversation and Google Messages pairing'
    return 'unknown', 'Host could not verify delivery; check the group before manually resending'


def tick(store=None, *, now=None, context=calendar_context, read=None, deliver=send_group, ready=group_ready, jitter=None):
    store = store or TrainerStore()
    fixed_now = now
    clock = lambda: time.time() if fixed_now is None else fixed_now
    now = clock()
    read = read or glofox_api.upcoming_0830_classes
    jitter = random.uniform(-120, 120) if jitter is None else jitter
    count = 0
    for owner in store.owners():
        cfg = store.claim(owner, now=now)
        if not cfg:
            continue
        try:
            ctx = context(owner, cfg['bot_id'])
            snapshot = read(now=datetime.fromtimestamp(now, EASTERN), days=cfg['horizon_days'])
            changes = store.apply_read(cfg, snapshot, now=clock(), jitter=jitter)
            count += 1
            if cfg['delivery'] == 'group':
                reason = ready(owner, cfg['bot_id'])
                if reason:
                    store.delivery_blocked(cfg, reason)
                    continue
            for change in changes:
                # Revalidate Bot authority immediately before each dispatch.
                ctx = context(owner, cfg['bot_id'])
                attempt = store.begin_delivery(cfg, change['id'], now=clock())
                if attempt:
                    try:
                        state, detail = deliver(cfg, attempt, ctx)
                    except Exception:
                        state, detail = 'unknown', 'Delivery interrupted; check the group before resending'
                    store.finish_delivery(attempt, state, detail, now=clock())
        except Exception as exc:
            # Provider text/credentials never enter saved error messages.
            store.read_failed(cfg, f'Monitor read unavailable ({type(exc).__name__}); previous trainers preserved', now=clock())
        finally:
            store.release(cfg)
    return count


def run(shutdown_event):
    store = TrainerStore()
    while not shutdown_event.is_set():
        try:
            tick(store)
        except Exception as exc:
            print(f'[level6-trainers] unavailable: {type(exc).__name__}', file=sys.stderr, flush=True)
        shutdown_event.wait(30)
