"""Fixed Level 6 member-portal booking wire; no credentials or arbitrary APIs."""
import json
import re
from datetime import datetime
from urllib.parse import urlsplit

import booking_schema as common

ACTION = 'workout_browser'
BRANCH = '67239a5afaac4eea10045d29'
URL = 'https://level6training.com/book-a-class'
HOSTS = ('level6training.com', 'app.glofox.com', 'api.glofox.com', 'gf-api.aws.glofox.com')

FAILURE_PHASES = {
    'adapter': 'starting the workout adapter',
    'navigation': 'opening the booking website',
    'member_session': 'verifying the saved member session',
    'snapshot': 'reading the class controls',
    'replay': 'replaying the class/date selection',
    'review': 'checking the booking review',
    'submission': 'submitting the approved booking',
    'receipt': 'verifying the booking receipt',
}


def browser_failure(exc):
    """Structural evidence only: never stringify errors, page data or locals."""
    from pathlib import Path
    phase = getattr(exc, 'kyrex_workout_phase', 'adapter')
    if not isinstance(phase, str) or phase not in FAILURE_PHASES: phase = 'adapter'
    kind = type(exc).__name__
    if kind not in {'ValueError', 'TypeError', 'KeyError', 'IndexError', 'AttributeError',
                    'RuntimeError', 'TimeoutError', 'Error', 'ImportError',
                    'ModuleNotFoundError', 'OSError', 'PermissionError'}:
        kind = 'Exception'
    detail = {'phase': phase, 'error_type': kind, 'location': 'unavailable'}
    trace = exc.__traceback__
    while trace is not None:
        name = Path(trace.tb_frame.f_code.co_filename).name
        if name in {'browser_operator.py', 'workout_booking_driver.py', 'booking_driver.py',
                    'workout_booking_schema.py', 'booking_schema.py'}:
            detail['location'] = name + ':' + str(trace.tb_lineno)
        trace = trace.tb_next
    step = getattr(exc, 'kyrex_workout_step', None)
    if type(step) is int and 0 <= step < 18: detail['step'] = step + 1
    return detail


def log_browser_failure(exc):
    import sys
    detail = browser_failure(exc)
    try:
        print('KYREX_WORKOUT_FAILURE:' + json.dumps(detail, sort_keys=True), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass  # A closed log stream must not change the booking outcome.
    return detail


def guest_bootstrap(url, method, body):
    """The portal's public, credential-free schedule bootstrap; never a booking."""
    return (method == 'POST' and url == 'https://api.glofox.com/2.0/login'
            and body == {'branch_id': BRANCH, 'login': 'GUEST', 'password': 'GUEST'})


def browser_problem(reason, *, include_detail=False):
    """Classify local host failures without echoing exception/page secrets."""
    text = str(reason or '')[:12000].lower()
    import browser_diagnostics
    code = browser_diagnostics.failure(reason)
    if code in {'cancelled', 'dispatch_timeout', 'dispatch_unavailable', 'dispatch_state_unreadable', 'dispatch_missing', 'dispatch_terminal', 'connection_lost'}:
        return browser_diagnostics.explain(reason)
    choices = (
        ('one task at a time', 'Your Browser Host is busy with another task. Let it finish before asking again.'),
        ('manualcontrolactive', 'The Browser Bot profile is still in manual control. End its private viewer before asking again.'),
        ('browserchannelunavailable', 'Kyrex Cloud cannot reach the Browser Host connection. Reconnect the host agent and check its connection status.'),
        ('timed out', 'The Browser Host did not finish before the timeout. Check its logs and connection status.'),
        ('timeout', 'The Browser Host did not finish before the timeout. Check its logs and connection status.'),
        ('hostunavailable', 'The Browser Host connection is unavailable. Check that its agent is running and connected.'),
        ("not on this host's allowlist", 'The booking site is blocked by the Browser Bot or Host site permissions.'),
        ("not in this bot's browser allowlist", 'The booking site is blocked by the Browser Bot or Host site permissions.'),
        ('no browser allowlist', 'The Browser Bot has no site permissions configured.'),
        ('no site allowlist', 'The Browser Host has no site permissions configured.'),
        ('allow the level 6', 'The Level 6 booking site is blocked by the Browser Bot or Host site permissions.'),
        ('sign in to your glofox', 'The Browser Bot profile is not signed into Glofox. Sign in through its private viewer, then end the viewer.'),
        ('verified adapter', 'The Glofox portal uses a booking flow Kyrex does not support yet. Its member booking adapter needs updating.'),
        ('verified booking adapter', 'The Glofox portal uses a booking flow Kyrex does not support yet. Its member booking adapter needs updating.'),
        ('unsupported payment', 'The portal request requires unsupported payment, guests or waitlist access.'),
        ('portal must show', 'The portal did not show verified free, membership or credit terms for this class.'),
        ('not a supported class selection control', 'Kyrex could not use the portal\'s class/date selector. Its browser adapter needs updating.'),
        ('workout control changed', 'The class/date controls changed during preparation. Fresh class options are needed.'),
        ('booking control changed', 'The selected class/date control is no longer available. Fresh class options are needed.'),
        ('booking frame changed', 'The member portal changed during preparation. Fresh class options are needed.'),
        ('member portal changed', 'The member portal changed during preparation. Fresh class options are needed.'),
        ('membership or credit terms changed', 'The membership or credit terms changed before booking. A new review is needed.'),
        ('portal is unavailable or blocked', 'The Level 6 member portal could not be reached or was blocked by site permissions.'),
        ('open the level 6 member portal', 'The Level 6 member portal was not found in this Browser Bot profile.'),
        ('browser tasks must be json', 'The Browser Host does not recognize the workout workflow. Rebuild its agent with the current Kyrex code.'),
        ("no module named 'workout_booking", 'The Browser Host is missing the workout adapter. Rebuild its agent with the current Kyrex code.'),
        ('booking requires the owner', 'The booking task does not match the owner Browser Bot profile. Check its host binding.'),
        ('not authorized', 'The workout browser authorization is no longer active. Start a fresh workout request.'),
        ('without a result', 'The browser operator stopped without a result. Check the Browser Host logs.'),
        ('could not start browser operator', 'The Browser Host could not start its browser operator. Check its logs.'),
    )
    for marker, message in choices:
        if marker in text:
            ref = browser_diagnostics.reference(reason)
            return message + (f' Diagnostic: {ref}.' if ref else '')
    ref = browser_diagnostics.reference(reason)
    message = 'The workout browser step failed without a verified result. Check the Browser Host logs for this attempt.'
    if include_detail:
        # Only use freshly projected structural evidence, never caller strings.
        safe = browser_failure(reason) if isinstance(reason, BaseException) else None
        if safe is not None:
            message += ' Failed while ' + FAILURE_PHASES[safe['phase']] + '.'
            message += ' ' + safe['error_type'] + ' at ' + safe['location'] + '.'
    return message + (f' Diagnostic: {ref}.' if ref else '')


def portal_url(url):
    parsed = urlsplit(url)
    return (parsed.scheme == 'https' and parsed.netloc == 'app.glofox.com'
            and parsed.path == '/portal/' and not parsed.username and not parsed.password
            and re.match(r'^/branch/' + BRANCH + r'(?:/|$)', parsed.fragment))


def request_template(url, method, body, identity):
    """Only the documented member endpoint with explicit no-waitlist intent."""
    parsed = urlsplit(url)
    paths = {'api.glofox.com': '/2.3/branches/' + BRANCH + '/bookings',
             'gf-api.aws.glofox.com': '/prod/2.3/branches/' + BRANCH + '/bookings'}
    if (method != 'POST' or parsed.scheme != 'https'
            or parsed.netloc not in paths or parsed.path != paths.get(parsed.netloc)
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError('This portal uses a booking endpoint that needs a verified adapter.')
    allowed = {'model', 'model_id', 'guest_bookings', 'join_waiting_list', 'charge',
               'user_id', 'branch_id', 'payment_method', 'price', 'pay_gym'}
    if (not isinstance(body, dict) or set(body) - allowed or body.get('model') != 'event'
            or not re.fullmatch(r'[a-f0-9]{24}', str(body.get('model_id', '')))
            or body.get('join_waiting_list') is not False
            or type(body.get('guest_bookings', 0)) is not int or body.get('guest_bookings', 0) != 0
            or body.get('branch_id', BRANCH) != BRANCH
            or body.get('price', 0) != 0 or isinstance(body.get('price', 0), bool)
            or body.get('pay_gym', False) is not False
            or body.get('payment_method', 'membership') not in {'membership', 'credits', 'credit', 'free'}
            or 'charge' in body and type(body['charge']) is not bool
            or 'user_id' in body and not re.fullmatch(r'[a-f0-9]{24}', str(body['user_id']))
            or not re.fullmatch(r'[a-f0-9]{64}', str(identity))):
        raise ValueError('The native booking request requires unsupported payment, guests or waitlist access.')
    return {'url': url, 'method': method, 'body': body, 'identity': identity}


def task(value):
    allowed = {'id', 'nonce', 'stage', 'url', 'details', 'steps', 'expected', 'submit', 'request'}
    if (not isinstance(value, dict) or set(value) - allowed
            or not re.fullmatch(r'[a-f0-9]{32}', str(value.get('id', '')))
            or not re.fullmatch(r'[a-f0-9]{64}', str(value.get('nonce', '')))
            or value.get('stage') not in {'prepare', 'submit'} or value.get('url') != URL):
        raise ValueError('Invalid Level 6 browser task.')
    details = value.get('details', {})
    if not isinstance(details, dict) or set(details) != {'date', 'time', 'event_id', 'class_name', 'trainer_name', 'start'}:
        raise ValueError('Invalid Level 6 class details.')
    common.details({k: details[k] for k in ('date', 'time')})
    if (not re.fullmatch(r'[a-f0-9]{24}', str(details['event_id']))
            or details['class_name'] != 'Group Fitness Class' or type(details['start']) is not int):
        raise ValueError('The workout is not a verified Level 6 class.')
    common.clean(details['trainer_name'], 120)
    from zoneinfo import ZoneInfo
    local = datetime.fromtimestamp(details['start'], ZoneInfo('America/New_York'))
    if local.strftime('%Y-%m-%d %H:%M') != details['date'] + ' ' + details['time']:
        raise ValueError('The class timestamp does not match the requested time.')
    steps = value.get('steps')
    if not isinstance(steps, list) or len(steps) > 18:
        raise ValueError('Too many workout preparation steps.')
    for step in steps:
        if not isinstance(step, dict) or set(step) - {'op', 'control', 'field', 'value'}:
            raise ValueError('Invalid workout preparation step.')
        common.control(step.get('control'))
        if not portal_url(step['control']['frame']):
            raise ValueError('Workout controls must belong to the Level 6 portal.')
        if step.get('op') in {'fill', 'select'}:
            if step.get('field') not in {'date', 'time'} or not common.matches(
                    step['field'], step.get('value'), details[step['field']]):
                raise ValueError('Workout form values must match your requested date and time.')
        elif step.get('op') != 'click' or step.get('field') or step.get('value'):
            raise ValueError('Unsupported workout preparation action.')
    if value['stage'] == 'submit':
        common.control(value.get('submit'))
        if not portal_url(value['submit']['frame']):
            raise ValueError('The submit control left the Level 6 portal.')
        if not re.fullmatch(r'[a-f0-9]{64}', str(value.get('expected', ''))):
            raise ValueError('Workout submission needs an exact owner review.')
        request = value.get('request', {})
        request_template(request.get('url', ''), request.get('method'), request.get('body'), request.get('identity'))
        if request['body']['model_id'] != details['event_id']:
            raise ValueError('The booking request targets a different class.')
    elif any(k in value for k in ('submit', 'expected', 'request')):
        raise ValueError('Workout preparation cannot approve submission.')
    return value


def wire(text):
    value = json.loads(text)
    if set(value) == {ACTION}:
        return ACTION, task(value[ACTION])
    if set(value) == {common.ACTION}:
        return common.ACTION, common.task(value[common.ACTION])
    raise ValueError('Invalid fixed booking task.')
