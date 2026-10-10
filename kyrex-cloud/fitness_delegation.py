"""Read-only fitness delegation through the existing durable task and Chat paths."""
import asyncio
import re
import sys
from pathlib import Path


def analysis_text(text):
    return re.sub(r'^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:ask|tell|have)\s+(?:the\s+)?workout\s+bot\s+(?:to\s+)?', '', str(text or ''), flags=re.I).strip()


def contextual_request(text, messages):
    """Keep an explicit previous sleep date when the owner asks for its graph.

    Only owner text contributes; assistant replies and record/provider text are
    not instructions or a source of requested dates.
    """
    query = analysis_text(text)
    if query == str(text).strip() or not re.search(r'\bsleep\b', query, re.I):
        return text
    if re.search(r'\b(?:today|tonight|yesterday|last|past|week|month|days|nights)\b|\d{4}-\d{2}-\d{2}', query, re.I):
        return text
    previous = next((m.get('content', '') for m in reversed(messages)
                     if m.get('role') == 'user'), '')
    if is_read_request(previous) and re.search(r'\bsleep\b.*\blast night\b', previous, re.I):
        return query + ' for last night'
    return text


def is_read_request(text):
    text = analysis_text(text)
    if not text or len(text) > 8000:
        return False
    # Development, messages, scheduling and profile mutations keep their own
    # routes. A mention of sleep/workouts alone is not a wearable read request.
    if re.search(r'\b(implement|debug|bug|code|coding|feature|firebase|repository|repo|deploy|fix|save|forget|update|remember|send|delete|schedule|calendar|email)\b', text, re.I):
        return False
    subject = re.search(r'\b(?:my\s+(?:sleep|workouts?|fitness|readiness|recovery|activity|heart\s+rate|hrv)|(?:sleep|workout|fitness|wearable)\s+(?:data|stats|profile))\b', text, re.I)
    return bool(subject and re.search(r'\b(how|show|read|pull|check|review|graph|chart|plot|analy[sz]e|evaluate|compare|tell|explain|summari[sz]e)\b', text, re.I))


def eligible(bot):
    import serve
    try:
        return (serve.effective_permissions(bot.get('policy')).get('fitness:read') == 0
                and not serve.is_writable_bot_policy(bot.get('policy'))
                and not serve.coordinator_granted(bot))
    except Exception:
        return False


def select_target(owner, coordinator_id):
    import bots
    import delegation
    candidates = []
    for bot in bots.load_bots().values():
        if bot.get('owner') != owner or bot.get('id') == coordinator_id or not eligible(bot):
            continue
        try:
            candidates.append(delegation.resolve_delegation_target(owner, bot['id']))
        except delegation.DelegationError:
            continue
    if len(candidates) != 1:
        raise delegation.DelegationError('Start one Workout Bot with its fitness read grant and configured model.' if not candidates else
            'More than one fitness Bot is available. Choose which Bot should handle this request.')
    return candidates[0]


def result_cards(task, rec, owner):
    """Project only a completed linked same-owner fitness task's native cards."""
    if (task.get('executor_prefix') != 'fitness' or rec.get('executor_prefix') != 'fitness' or task.get('status') != 'done'
            or rec.get('owner') != owner or task.get('chat_id') != owner
            or task.get('bot_id') != rec.get('target_bot_id')
            or task.get('parent_delegation_id') != rec.get('delegation_id')
            or rec.get('task_id') != task.get('task_id')):
        return {}
    result = task.get('result') or {}
    if not isinstance(result, dict) or result.get('mode') != 'fitness':
        return {}
    return {key: result[key] for key in ('sleep_report', 'workout_report')
            if isinstance(result.get(key), dict) and result[key].get('version') == 1}


def run_task(ctx, owner, text, task_id, conversation_id, send, on_progress=None, on_result=None):
    """Recheck binding and lifecycle, then use the target's own Chat/model.

    No repository workflow, global model, profile write or recursive delegation
    is available. The worker owns heartbeat, cancellation and terminal status.
    """
    import bots
    import serve
    from task_store import CloudTaskStore
    store = CloudTaskStore()
    task = store.get(task_id) or {}
    rec = store.get_delegation(task.get('parent_delegation_id')) or {}
    target = bots.load_bots().get(getattr(ctx, 'bot_id', '')) or {}
    coordinator = bots.load_bots().get(rec.get('coordinator_bot_id')) or {}
    if (not owner or getattr(ctx, 'bot_owner', '') != owner or target.get('owner') != owner
            or not bots.is_running(target) or not eligible(target)
            or coordinator.get('owner') != owner or not serve.coordinator_granted(coordinator)
            or task.get('status') != 'running' or task.get('chat_id') != owner
            or task.get('bot_id') != target.get('id') or task.get('executor_prefix') != 'fitness'
            or rec.get('owner') != owner or rec.get('target_bot_id') != target.get('id')
            or rec.get('task_id') != task_id or task.get('conversation_id') != conversation_id
            or rec.get('executor_prefix') != 'fitness' or rec.get('depth') != 1
            or task.get('task_text') != text or rec.get('task_text') != text
            or rec.get('parent_conversation_id') == conversation_id
            or not is_read_request(text)):
        raise ValueError('Fitness delegation binding or read permission is unavailable.')
    backend = str(Path(__file__).parent / 'web' / 'backend')
    if backend not in sys.path: sys.path.insert(0, backend)
    import chat_service
    conv = chat_service.get_conversation(owner, conversation_id)
    if not conv or conv.get('bot_id') != target['id']:
        raise ValueError('Workout Bot conversation is unavailable.')
    if store.is_cancel_requested(task_id): return
    if on_progress: on_progress({'stage': 'reading', 'message': 'Workout Bot is reading your connected fitness data.'})

    async def collect():
        cancel = asyncio.Event()
        async def watch():
            while not cancel.is_set():
                if store.is_cancel_requested(task_id): cancel.set(); return
                await asyncio.sleep(.1)
        watcher = asyncio.create_task(watch())
        terminal = None
        try:
            async for frame in chat_service.stream_chat(owner, conversation_id, analysis_text(text),
                    request_id=f'fitness-{task_id}', cancel_event=cancel, fitness_read_only=True):
                if frame.get('type') == 'status': terminal = frame
            return terminal
        finally:
            watcher.cancel()
            try: await watcher
            except asyncio.CancelledError: pass
    try:
        terminal = asyncio.run(collect()) or {}
    finally:
        chat_service.close_engine_session(owner, conversation_id)
    if store.is_cancel_requested(task_id) or terminal.get('status') == 'cancelled': return
    if terminal.get('status') != 'complete':
        raise ValueError('Workout Bot could not complete the fitness read.')
    result = {'mode': 'fitness', 'status': 'no_changes', 'count': 1,
              'final_response': str(terminal.get('content') or 'Your fitness chart is ready.')[:12000]}
    for key in ('sleep_report', 'workout_report'):
        if terminal.get(key): result[key] = terminal[key]
    if on_result: on_result(result)
    send(owner, result['final_response'])
