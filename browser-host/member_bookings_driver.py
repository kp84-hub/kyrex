"""Read the portal's member bookings. No booking/cancel/payment write path."""
import json
import re
import time
from urllib.parse import urlsplit

import member_bookings_schema as schema
import workout_booking_schema as workout
from workout_booking_driver import identity, member_session


def session_refresh(request):
    """Permit only native session maintenance, never a generated login/write.

    Endpoint and exact body shape are observed in Glofox's public portal.
    Tokens remain in the existing page/host and are never read from storage,
    generated, logged, or returned to Cloud. Redirected requests are refused.
    """
    if (request.url != schema.REFRESH_URL or request.method != 'POST'
            or request.redirected_from is not None):
        return False
    try:
        if not workout.portal_url(request.frame.url):
            return False
        body = json.loads(request.post_data or 'null')
    except (ValueError, AttributeError):
        return False
    return (isinstance(body, dict) and set(body) == {'refresh_token'}
            and isinstance(body['refresh_token'], str)
            and 0 < len(body['refresh_token']) <= 8192
            and not any(ord(c) < 32 for c in body['refresh_token']))


def run(driver, proto, spec, *, allowlist, expected_request=None):
    from browser_operator import domain_allowed
    spec = schema.parse(spec)
    if expected_request is not None:
        workout.reviewed_request(expected_request)
    allowed = lambda u: domain_allowed(u, allowlist)[0] and domain_allowed(u, list(schema.READER_HOSTS))[0]
    context, page = driver._context, driver._page
    if context.service_workers or not allowed(schema.URL):
        raise ValueError('Member bookings require a managed, read-only browser session.')
    if not proto.operation('browser.read', schema.URL, 'Read existing member bookings only', ''):
        raise ValueError('Member bookings read denied.')
    seeds = []
    blocked_refresh = []
    account_changed = []

    def guard(route):
        req = route.request
        bootstrap = False
        try:
            bootstrap = workout.guest_bootstrap(req.url, req.method, json.loads(req.post_data or 'null'))
        except ValueError:
            pass
        refresh = session_refresh(req)
        refresh_preflight = req.url == schema.REFRESH_URL and req.method == 'OPTIONS' and req.redirected_from is None
        refresh_host = urlsplit(req.url).hostname == 'auth.glofox.com'
        denied = (not allowed(req.url)
                or (refresh_host and not (refresh or refresh_preflight))
                or req.method not in {'GET', 'HEAD', 'OPTIONS'} and not (bootstrap or refresh)
                or re.search(r'[?&](?:action|op|method)=(?:cancel|delete|submit|purchase|logout)(?:&|$)', req.url, re.I)
                or re.search(r'/(?:cancel|delete|logout|signout|checkout|purchase|submit|confirm|attendances)(?:/|$)',
                             urlsplit(req.url).path, re.I))
        if denied:
            if req.url == schema.REFRESH_URL:
                blocked_refresh.append(True)
            route.abort()
            return
        member = schema.native_member(req.url) if req.method == 'GET' else None
        if member:
            # Native authorization stays on this host, never in Cloud or logs.
            headers = req.all_headers()
            if not headers.get('authorization'):
                route.abort()
                return
            if expected_request is not None and (
                    identity(req) != expected_request['identity']
                    or expected_request['body'].get('user_id', member) != member):
                account_changed.append(True)
                route.abort()
                return
            seeds.append((req.url, member, {k: v for k, v in headers.items()
                if k in {'authorization', 'x-api-key', 'x-glofox-api-token', 'x-glofox-branch-id'}}))
        route.fallback()

    context.route('**/*', guard)
    context.route_web_socket('**/*', lambda socket: socket.close())
    response = page.goto(schema.URL, wait_until='domcontentloaded', timeout=15000)
    if response is None or response.status >= 400 or not allowed(page.url):
        raise ValueError('The member portal is unavailable.')
    state = member_session(page, allowed)
    if state != 'signed_in':
        raise schema.ReadFailure('refresh_blocked' if blocked_refresh else state)
    frames = [f for f in page.frames if workout.portal_url(f.url) and allowed(f.url)]
    if len(frames) != 1:
        raise ValueError('The supported member portal was not found.')
    portal = frames[0]
    # Only observed self-profile/booking-list controls. All HTTP writes and
    # WebSockets remain blocked during clicks, reads and context teardown.
    for labels in (('My bookings', 'Bookings'), ('Profile', 'My profile'), ('My bookings', 'Bookings')):
        if seeds: break
        targets = portal.get_by_text(re.compile(r'^(?:' + '|'.join(labels) + r')$', re.I)).filter(visible=True)
        if targets.count() == 1:
            targets.click(timeout=3000)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not seeds:
                page.wait_for_timeout(100)
    if account_changed:
        raise ValueError('The member-bookings account changed after booking review.')
    if not seeds:
        raise schema.ReadFailure('missing_read')
    identities = {schema.session_identity(headers['authorization'], member) for _, member, headers in seeds}
    if len(identities) != 1:
        raise ValueError('The member-bookings account was ambiguous.')
    native, member, headers = seeds[0]
    rows, ids, count, total = [], set(), 0, None
    for number in range(1, schema.MAX_PAGES + 1):
        # Fixed GET through the page uses the SAME network guard. Never use
        # APIRequestContext/route.fetch, redirects or an unobserved channel.
        response = portal.evaluate('''async ({url,headers,limit}) => {
          const controller=new AbortController();
          const timer=setTimeout(()=>controller.abort(),10000);
          try {
            const response=await fetch(url,{method:'GET',headers,redirect:'error',signal:controller.signal});
            if(response.status!==200) return null;
            const reader=response.body.getReader(); let length=0, chunks=[];
            while(true) { const {done,value}=await reader.read(); if(done) break;
              length+=value.length; if(length>limit) {await reader.cancel(); return null;} chunks.push(value); }
            const body=new Uint8Array(length); let offset=0;
            for(const part of chunks) {body.set(part,offset);offset+=part.length;}
            return JSON.parse(new TextDecoder().decode(body));
          } catch { return null; } finally {clearTimeout(timer);}
        }''', {'url': schema.read_url(native, spec, number), 'headers': headers, 'limit': schema.MAX_BYTES})
        batch = schema.page_rows(response, member, spec, number)
        if expected_request is not None:
            # Reconciliation needs explicit confirmation; the ordinary reader
            # retains its existing compatibility with omitted confirmation flags.
            confirmed = {raw['_id'] for raw in response['data']
                         if raw.get('confirmed') is True and raw.get('is_from_waiting_list') is not True}
            batch = [row for row in batch if row['reference'] in confirmed]
        if total is None: total = response['total_count']
        if total != response['total_count']:
            raise ValueError('The member-bookings list changed while paging.')
        for raw in response['data']:
            ident = raw.get('_id')
            if not isinstance(ident, str) or ident in ids:
                raise ValueError('The member-bookings pages overlap or lack references.')
            ids.add(ident)
        count += len(response['data']); rows.extend(batch)
        if not response['has_more']:
            if count != total: raise ValueError('The member-bookings list was incomplete.')
            if account_changed or len({schema.session_identity(h['authorization'], m) for _, m, h in seeds}) != 1:
                raise ValueError('The member-bookings account changed during the read.')
            return schema.result(rows, next(iter(identities)), spec)
        if not response['data']:
            break
    raise ValueError('The member-bookings list exceeds the read limit.')
