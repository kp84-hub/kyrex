"""Owner-run, write-blocked portal request capture; never grants booking authority.

Run inside the existing Browser Host container through stdin. No image rebuild,
model, Cloud submission, generated API call, or profile reset is required.
"""
import argparse
import json
import os
import re
import signal
from pathlib import Path
from urllib.parse import urlsplit

import booking_schema as common
import host_allowlist
import manual_mode
import profiles
import workout_booking_driver as workout
import workout_booking_schema as schema


FIELDS = ('model', 'model_id', 'guest_bookings', 'join_waiting_list', 'charge',
          'user_id', 'branch_id', 'payment_method', 'price', 'pay_gym')
SEGMENTS = {'2.0', '2.1', '2.2', '2.3', 'prod', 'branches', 'bookings', 'login',
            'users', 'events', 'agreements', 'send', 'purchase', 'payments', 'price'}


def request_shape(request):
    """Fixed labels/types only. Never emit query, headers, IDs or unknown keys."""
    url = urlsplit(request.url)
    segments = [part if part in SEGMENTS else ':redacted' for part in url.path.split('/') if part]
    try:
        body = json.loads(request.post_data or '')
    except (TypeError, ValueError):
        body = None
    def kind(value):
        return {str: 'string', bool: 'boolean', int: 'number', float: 'number',
                dict: 'object', list: 'array', type(None): 'null'}.get(type(value), 'other')
    result = {'method': request.method if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} else 'other',
              'host': url.netloc if url.netloc in schema.HOSTS[2:] else 'other',
              'path': '/' + '/'.join(segments[:12]), 'query_present': bool(url.query),
              'body_type': kind(body)}
    if isinstance(body, dict):
        result['fields'] = {key: kind(body[key]) for key in FIELDS if key in body}
        result['other_field_count'] = len(set(body) - set(FIELDS))
        result['event_model'] = body.get('model') == 'event'
        for key in ('join_waiting_list', 'charge', 'pay_gym'):
            if key in body:
                result[key] = body[key] if type(body[key]) is bool else 'invalid'
        for key in ('guest_bookings', 'price'):
            if key in body:
                result[key] = ('zero' if type(body[key]) in {int, float} and body[key] == 0 else 'nonzero_or_invalid')
        if 'branch_id' in body: result['requested_branch'] = body['branch_id'] == schema.BRANCH
        if 'payment_method' in body:
            value = body['payment_method']
            result['payment_method'] = value if type(value) is str and value in {'membership', 'credits', 'credit', 'free', 'cash', 'card', 'credit_card'} else 'other'
    return result


def inspect(driver, allowlist, day, clock):
    """Observe only an exact visible day/time. All member writes are aborted."""
    common.details({'date': day, 'time': clock})
    allowed = lambda url: host_allowlist.domain_allowed(url, allowlist)[0]
    if not allowed(schema.URL): return {'status': 'site_permissions'}
    context, page = driver._context, driver._page
    if context.service_workers: return {'status': 'service_worker_present'}
    evidence, phase = [], ['opening']

    def guard(route):
        request = route.request
        if not allowed(request.url):
            route.abort(); return
        if request.method in {'GET', 'HEAD', 'OPTIONS'}:
            if re.search(r'/(?:cancel|delete|logout|signout|checkout|purchase|submit|confirm)(?:/|$)', urlsplit(request.url).path, re.I):
                route.abort(); return
            route.fallback(); return
        try: body = json.loads(request.post_data or '')
        except (TypeError, ValueError): body = None
        if schema.guest_bootstrap(request.url, request.method, body):
            route.fallback(); return
        if urlsplit(request.url).netloc in schema.HOSTS[2:] and len(evidence) < 12:
            evidence.append({'phase': phase[0], **request_shape(request)})
        route.abort()

    # Keep guards installed through context teardown, including late timers.
    context.route('**/*', guard)
    context.route_web_socket('**/*', lambda socket: socket.close())
    driver.navigate(schema.URL)
    if workout.member_session(page, allowed) != 'signed_in':
        return {'status': 'member_session_unverified', 'requests': evidence}
    snap = workout.observed(driver, allowed)
    day_control = schema.requested_day_control(snap, {'date': day}, [])
    if day_control is None:
        return {'status': 'requested_day_not_unique_or_visible', 'requests': evidence}
    phase[0] = 'calendar'
    workout.base.locate(driver, day_control, allowed).click(timeout=10000)
    page.wait_for_timeout(1000)
    hour, minute = map(int, clock.split(':'))
    time_label = re.compile(r'(?<!\d)' + str(hour % 12 or 12) + ':' + f'{minute:02d}'
                            + r'\s*' + ('pm' if hour >= 12 else 'am') + r'\b', re.I)
    for _ in range(2):
        snap = workout.observed(driver, allowed)
        candidates = [c for c in snap['controls'] if schema.portal_url(c['frame']) and not c['disabled']
                      and re.fullmatch(r'book|confirm(?: booking)?', c['label'], re.I)
                      and 'Group Fitness Class' in snap['contexts'].get(c['ref'], '')
                      and time_label.search(snap['contexts'].get(c['ref'], ''))]
        if len(candidates) != 1:
            return {'status': 'class_control_not_unique_or_visible', 'requests': evidence}
        phase[0] = 'class'
        workout.base.locate(driver, candidates[0], allowed).click(timeout=10000)
        page.wait_for_timeout(1000)
        if evidence:
            return {'status': 'writes_blocked', 'requests': evidence}
    return {'status': 'no_request_observed', 'requests': evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--bot', required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--time', required=True)
    args = parser.parse_args()
    common.details({'date': args.date, 'time': args.time})
    # Refuse arbitrary/missing profiles and owner mismatches; do not create one.
    if os.environ.get('KYREX_HOST_OWNER') != args.owner:
        raise SystemExit('Host owner mismatch; capture refused.')
    profile = profiles.profile_dir(args.owner, args.bot)
    if not profile.is_dir(): raise SystemExit('Existing profile not found; capture refused.')
    allowlist = host_allowlist.parse_allowlist(os.environ.get('KYREX_HOST_ALLOWLIST'))
    state = manual_mode.state_dir()
    if manual_mode.is_any_active(root=state, kind=manual_mode.KIND_MANUAL):
        raise SystemExit('End the private viewer before capture.')
    from browser_operator import PlaywrightDriver
    with manual_mode.acquire(args.owner, args.bot, kind=manual_mode.KIND_AUTOMATION, ttl=120, root=state):
        driver = PlaywrightDriver(Path(profile), managed=True)
        driver.diagnostic_mode = True
        def deadline(*_): raise TimeoutError('Capture deadline')
        signal.signal(signal.SIGALRM, deadline)
        signal.alarm(90)
        try:
            driver.open()
            result = inspect(driver, allowlist, args.date, args.time)
        except Exception as exc:
            result = {'status': 'capture_failed', 'failure': schema.browser_failure(exc)}
        finally:
            driver.close()
            signal.alarm(0)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
