"""Read-only browser probe. Never clicks, types, captures or submits a form."""
import re
import json
from urllib.parse import urlsplit

import browser_diagnostics as schema


def run(driver, proto, spec, *, allowlist, stages):
    from browser_operator import domain_allowed
    url = schema.parse(spec)['url']
    import workout_booking_schema as workout
    allowed = lambda u: domain_allowed(u, allowlist)[0] and domain_allowed(u, ['*'])[0]
    if not allowed(url): return schema.host_result('site_permissions', stages=stages)
    if not proto.operation('browser.navigate', url, 'Test website access', ''):
        return schema.host_result('site_permissions', stages=stages)
    context = getattr(driver, '_context', None)
    page = getattr(driver, '_page', None)
    if context is None or page is None:
        return schema.host_result('browser_launch_failed', stages=stages)
    # Existing workers could bypass interception; do not use such a profile.
    if context.service_workers:
        return schema.host_result('unsupported_portal', stages=stages)

    auth_blocked = False

    def guard(route):
        nonlocal auth_blocked
        req = route.request
        bootstrap = False
        if url == workout.URL and allowed(req.url):
            try: bootstrap = workout.guest_bootstrap(req.url, req.method, json.loads(req.post_data or 'null'))
            except ValueError: pass
        if ((req.method not in {'GET', 'HEAD', 'OPTIONS'} and not bootstrap) or not allowed(req.url)
                or re.search(r'/(cancel|delete|logout|signout|checkout|purchase|submit|confirm|bookings?)(?:/|\?|$)', urlsplit(req.url).path, re.I)):
            if (url == workout.URL and req.method == 'POST'
                    and req.url == 'https://api.glofox.com/2.0/login' and not bootstrap):
                auth_blocked = True
            route.abort()
        else:
            route.fallback()

    context.route('**/*', guard)
    try:
        # WebSocket messages are outside HTTP routing and must not become an
        # unobserved write channel during a nominally read-only test.
        context.route_web_socket('**/*', lambda socket: socket.close())
        response = page.goto(url, wait_until='domcontentloaded', timeout=15000)
        if not allowed(page.url) or response is None or response.status >= 400:
            return schema.host_result('site_unreachable', stages=stages)
        stages['site_access'] = 'passed'
        proto.progress({schema.ACTION: {'stage': 'site_access', 'state': 'passed'}})
        if not proto.operation('browser.read', page.url, 'Check sign-in readiness without reading account details', ''):
            return schema.host_result('site_permissions', stages=stages)
        # The existing studio adapter identifies its own member portal. Other
        # websites report sign-in as not checked, never a guessed green check.
        if url == workout.URL:
            import workout_booking_driver
            session = workout_booking_driver.member_session(page, allowed)
            if session != 'signed_in':
                code = ('session_verification_blocked' if auth_blocked else
                        'login_required' if session == 'signed_out' else session)
                return schema.host_result(code, stages=stages)
            stages['sign_in'] = 'passed'
        return schema.host_result(stages=stages)
    except Exception:
        return schema.host_result('site_unreachable', stages=stages)
    # The guard remains installed until run_actions closes this task-owned
    # context, so late page timers cannot write during teardown.
