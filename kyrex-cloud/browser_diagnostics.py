"""Secret-free browser health contracts shared by Cloud and enrolled hosts."""
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

ACTION = 'browser_diagnostic'
TIMEOUT = 75
STAGES = ('connection', 'dispatch', 'browser_launch', 'site_access', 'sign_in')
REFERENCE = re.compile(r'\bbd-[a-f0-9]{16}\b')

# Descriptions are fixed copy, never exception text or page content.
FAILURES = {
    'host_unbound': ('connection', 'Select a Browser Host for this Bot in Bots.'),
    'bot_inactive': ('dispatch', 'Start this Browser Bot in Bots before running a new test.'),
    'permission_revoked': ('dispatch', 'The Bot’s browser permission changed before this task ran.'),
    'host_offline': ('connection', 'The Browser Host is offline. Reconnect its agent.'),
    'connection_lost': ('connection', 'The Browser Host connection dropped during this task.'),
    'dispatch_unavailable': ('dispatch', 'Kyrex Cloud cannot reach the Browser Host dispatch connection.'),
    'dispatch_state_unreadable': ('dispatch', 'Cloud could not read the saved browser dispatch state.'),
    'dispatch_missing': ('dispatch', 'Cloud could not find the saved browser dispatch request.'),
    'dispatch_terminal': ('dispatch', 'The saved browser dispatch already ended. It was not sent again.'),
    'dispatch_timeout': ('dispatch', 'The browser dispatch reached its timeout before a result was recorded.'),
    'cancelled': ('dispatch', 'The browser task was cancelled.'),
    'profile_busy': ('browser_launch', 'The browser profile is busy. Finish its current task or close its private viewer.'),
    'host_update_required': ('dispatch', 'Update the Browser Host image to enable the browser connection test.'),
    'site_permissions': ('site_access', 'The website is blocked by the Bot or Host site permissions.'),
    'browser_launch_failed': ('browser_launch', 'The host received the task but could not launch its browser.'),
    'site_unreachable': ('site_access', 'The browser could not reach the requested website.'),
    'login_required': ('sign_in', 'Sign in through this Browser Bot’s private viewer, then close the viewer.'),
    'unsupported_portal': ('sign_in', 'The member portal could not be identified; sign-in was not verified.'),
    'failed': ('dispatch', 'The browser step failed. Check its diagnostic in Connections.'),
}


def build_version(value=None):
    value = str(os.environ.get('RAILWAY_GIT_COMMIT_SHA', '') if value is None else value)
    return value[:12] if re.fullmatch(r'[a-fA-F0-9]{7,40}', value) else 'unknown'


def target(value):
    """A public HTTPS page without query, credentials, fragment or effect URL."""
    if not isinstance(value, str) or len(value) > 1000 or any(ord(c) < 33 for c in value):
        raise ValueError('Enter a public HTTPS website URL without sign-in tokens.')
    try:
        p = urlsplit(value)
        import ipaddress
        host = p.hostname or ''
        if p.scheme != 'https' or not host or p.username or p.password or p.query or p.fragment or p.port not in (None, 443):
            raise ValueError()
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError('private address')
        except ValueError as exc:
            if str(exc) == 'private address':
                raise
            if '.' not in host or host.lower().rstrip('.').endswith(('.local', '.localhost', '.internal', '.lan', '.home')):
                raise ValueError()
        if re.search(r'(?:^|[/_-])(cancel|delete|logout|signout|checkout|purchase|submit|confirm|bookings?)(?:[/_-]|$)', p.path, re.I):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Use a public HTTPS page URL without query parameters or action links.') from None
    return value


def parse(value):
    if not isinstance(value, dict) or set(value) != {'url'}:
        raise ValueError('Browser tests accept only a website URL; no clicks or submission steps.')
    return {'url': target(value['url'])}


def wire(url):
    return json.dumps({ACTION: parse({'url': url})}, sort_keys=True)


def is_probe(task_text):
    try:
        value = json.loads(task_text)
        return isinstance(value, dict) and set(value) == {ACTION} and bool(parse(value[ACTION]))
    except (ValueError, TypeError):
        return False


def failure(reason):
    text = str(reason or '')[:12000].lower()
    if text in FAILURES: return text
    # More specific dispatch failures MUST precede the legacy HostUnavailable.
    checks = (
        (('browser bot is not running',), 'bot_inactive'),
        (('diagnostic permission is no longer active',), 'permission_revoked'),
        (('cancelled', 'canceled'), 'cancelled'),
        (('deadline expired', 'dispatch timed out'), 'dispatch_timeout'),
        (('dispatch state unreadable',), 'dispatch_state_unreadable'),
        (('dispatch request not found',), 'dispatch_missing'),
        (('dispatch already terminal',), 'dispatch_terminal'),
        (('browserchannelunavailable',), 'dispatch_unavailable'),
        (('connection lost', 'connection dropped', 'not connected'), 'connection_lost'),
        (('manualcontrolactive', 'one task at a time', 'browser is busy'), 'profile_busy'),
        (('browser_diagnostic', 'diagnostic capability'), 'host_update_required'),
        (('allowlist', 'site permissions', 'allow the level 6'), 'site_permissions'),
        (('sign in to your glofox', 'sign in to your member'), 'login_required'),
        (('failed to start', 'playwright is not available', 'browser launch', 'processsingleton'), 'browser_launch_failed'),
        (('net::', 'navigation timeout', 'could not reach'), 'site_unreachable'),
        (('hostunavailable', 'host is unavailable', 'host is offline'), 'host_offline'),
        (('timed out', 'timeout'), 'site_unreachable'),
    )
    for markers, code in checks:
        if any(m in text for m in markers):
            return code
    return 'failed'


def reference(value):
    match = REFERENCE.search(str(value or ''))
    return match[0] if match else ''


def explain(reason, ref=''):
    code = failure(reason)
    text = FAILURES[code][1]
    ref = reference(ref) or reference(reason)
    return text + (f' Diagnostic: {ref}.' if ref else '')


def host_result(code='', *, stages=None):
    code = code if code in FAILURES else ''
    return {'status': 'error' if code else 'no_changes', 'mode': ACTION,
            'diagnostic': {'code': code, 'stages': stages or {},
                           'host_build': build_version(os.environ.get('KYREX_HOST_BUILD_SHA', ''))},
            'final_response': FAILURES[code][1] if code else 'Browser connection test completed.'}


def report(row):
    """Project a durable dispatch into bounded, secret-free owner diagnostics."""
    result = row.get('result')
    if isinstance(result, str):
        try: result = json.loads(result)
        except ValueError: result = {}
    result = result if isinstance(result, dict) else {}
    detail = result.get('diagnostic') if result.get('mode') == ACTION else {}
    detail = detail if isinstance(detail, dict) else {}
    reason = row.get('error') or ''
    if not reason and result.get('status') == 'error':
        reason = ' '.join(str(e) for e in result.get('errors', [])[:3]) if isinstance(result.get('errors'), list) else ''
        reason = reason or result.get('final_response') or 'failed'
    code = detail.get('code') if detail.get('code') in FAILURES else failure(reason) if reason else ''
    status = row.get('status')
    if status in {'pending', 'executing'} and row.get('deadline_at'):
        try:
            if datetime.fromisoformat(row['deadline_at']) <= datetime.now(timezone.utc):
                code, status = 'dispatch_timeout', 'failed'
        except (ValueError, TypeError): pass
    stages = {}
    versions = {'cloud_build': 'unknown', 'host_build': build_version(detail.get('host_build', ''))}
    progress = row.get('progress') or []
    if isinstance(progress, str):
        try: progress = json.loads(progress)
        except ValueError: progress = []
    if isinstance(progress, list):
        for note in progress[-100:]:
            part = note.get(ACTION) if isinstance(note, dict) else None
            if not isinstance(part, dict): continue
            if part.get('stage') in STAGES and part.get('state') in {'passed', 'failed', 'not_checked'}:
                stages[part['stage']] = part['state']
            if 'cloud_build' in part: versions['cloud_build'] = build_version(part['cloud_build'])
            if 'host_build' in part and versions['host_build'] == 'unknown': versions['host_build'] = build_version(part['host_build'])
    for stage, state in (detail.get('stages') or {}).items() if isinstance(detail.get('stages'), dict) else []:
        if stage in STAGES and state in {'passed', 'failed', 'not_checked'}: stages[stage] = state
    if code: stages[FAILURES[code][0]] = 'failed'
    return {'reference': reference(row.get('dispatch_id')), 'bot_id': row.get('bot_id'),
            'host_id': row.get('host_id'), 'status': status, 'created_at': row.get('created_at'),
            'code': code, 'message': FAILURES[code][1] if code else 'Browser test completed.' if row.get('status') == 'done' else 'Browser test in progress.',
            'stages': [{'id': s, 'state': stages.get(s, 'not_checked')} for s in STAGES], **versions}
