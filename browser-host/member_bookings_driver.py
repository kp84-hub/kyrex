"""Read the portal's member bookings. No booking/cancel/payment write path."""
import json
import re
import time
from urllib.parse import urlsplit

import member_bookings_schema as schema
import workout_booking_schema as workout
from workout_booking_driver import member_session


def run(driver, proto, spec, *, allowlist):
    from browser_operator import domain_allowed
    spec = schema.parse(spec)
    allowed = lambda u: domain_allowed(u, allowlist)[0] and domain_allowed(u, list(schema.HOSTS))[0]
    context, page = driver._context, driver._page
    if context.service_workers or not allowed(schema.URL):
        raise ValueError('Member bookings require a managed, read-only browser session.')
    if not proto.operation('browser.read', schema.URL, 'Read existing member bookings only', ''):
        raise ValueError('Member bookings read denied.')
    seeds = []

    def guard(route):
        req = route.request
        bootstrap = False
        try:
            bootstrap = workout.guest_bootstrap(req.url, req.method, json.loads(req.post_data or 'null'))
        except ValueError:
            pass
        if (not allowed(req.url) or req.method not in {'GET', 'HEAD', 'OPTIONS'} and not bootstrap
                or re.search(r'[?&](?:action|op|method)=(?:cancel|delete|submit|purchase|logout)(?:&|$)', req.url, re.I)
                or re.search(r'/(?:cancel|delete|logout|signout|checkout|purchase|submit|confirm|attendances)(?:/|$)',
                             urlsplit(req.url).path, re.I)):
            route.abort()
            return
        member = schema.native_member(req.url) if req.method == 'GET' else None
        if member:
            # Native authorization stays on this host, never in Cloud or logs.
            headers = req.all_headers()
            if not headers.get('authorization'):
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
    if member_session(page, allowed) != 'signed_in':
        raise ValueError('Sign in through the Browser Bot private viewer, then close the viewer.')
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
    if not seeds:
        raise ValueError('The portal did not expose a supported My bookings read. No calendar events were added.')
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
            if len({schema.session_identity(h['authorization'], m) for _, m, h in seeds}) != 1:
                raise ValueError('The member-bookings account changed during the read.')
            return schema.result(rows, next(iter(identities)), spec)
        if not response['data']:
            break
    raise ValueError('The member-bookings list exceeds the read limit.')
