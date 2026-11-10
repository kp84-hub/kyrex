"""Present and answer the existing durable T2 review from one voice session."""
import asyncio

import chat_service
import delegation
from task_store import NONTERMINAL_STATUSES

APPROVE = {'approve', 'approve this request', 'confirm approval'}
REVIEW = 'review approval'
WAIT_SECONDS = 6
NO_REVIEW = 'There is no single approval under review. Say review approval to check what is waiting in this voice chat.'


def pending(store, owner, cid, task_id, approval_id=None):
    task = store.get(task_id) or {}
    if (task.get('chat_id') != owner or task.get('status') != 'awaiting_approval'
            or task.get('cancel_requested')):
        return None
    parent = task.get('parent_delegation_id')
    if parent:
        record = delegation.fetch_delegation(owner, parent, store=store) or {}
        if record.get('parent_conversation_id') != cid or record.get('task_id') != task_id:
            return None
    elif task.get('conversation_id') != cid:
        return None
    rows = store.pending_approvals_for_task(task_id)
    if len(rows) != 1:
        return None
    row = rows[0]
    if (row['tier'] != 2 or not row.get('token') or row.get('operator_reply')
            or approval_id is not None and row['approval_id'] != approval_id):
        return None
    return row


async def find(store, owner, cid, cancel, *, turn=None):
    records = store.list_delegations(owner=owner, parent_conversation_id=cid, parent_task_id=turn,
                                    task_status='awaiting_approval' if turn is None else None, limit=100)
    if len(records) == 100:
        return None
    tasks = {record['task_id'] for record in records if record.get('task_id')}
    if turn is None:
        tasks.update(task['task_id'] for task in store.tasks_for_conversation(cid, owner)
                     if task['status'] == 'awaiting_approval')
    deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
    while not cancel.is_set():
        reviews = [row for task_id in tasks if (row := pending(store, owner, cid, task_id))]
        if len(reviews) > 1:
            return None
        if reviews:
            return reviews[0]
        if (turn is None or asyncio.get_running_loop().time() >= deadline
                or not any(store.status(task_id) in NONTERMINAL_STATUSES for task_id in tasks)):
            return None
        await asyncio.sleep(.25)
    return None


def prompt(devices, owner, cid, secret, request_id, row):
    # Approval text is display data from the executor, never model input.
    token = row['token']
    detail = '\n'.join(dict.fromkeys(value.strip() for value in (row['summary'], row['detail']) if value))
    detail = chat_service.sanitize_assistant_text(detail.replace(token, '[redacted]'))
    if not detail or len(detail) > 1100:
        return {'type': 'status', 'status': 'complete',
                'content': 'Open the approval in Chat to review the full request.'}
    if not devices.remember_approval(owner, secret, cid, request_id, row['task_id'], row['approval_id']):
        return {'type': 'status', 'status': 'cancelled', 'content': ''}
    return {'type': 'status', 'status': 'complete', 'voice_approval': True,
            'content': f'Approval needed. {detail}\nAfter the tone, say confirm approval to proceed.'}


async def approve(store, owner, cid, review, cancel):
    if not review or cancel.is_set() or not pending(store, owner, cid, *review):
        return NO_REVIEW
    task_id, approval_id = review
    ok, _ = chat_service.approve_delegated_task(owner, task_id, approval_id=approval_id)
    if not ok:
        return 'That approval changed or was already answered. Say review approval to check the current request.'
    deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
    while not cancel.is_set():
        status, summary = chat_service._safe_result_summary(store, task_id)
        if status == 'done':
            return summary or 'The task finished. Open your voice chat for details.'
        if status in {'failed', 'cancelled'}:
            return f'The task {status}. Check your voice chat for details.'
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(.25)
    return 'I sent your approval. The task outcome is still pending in your voice chat.'
