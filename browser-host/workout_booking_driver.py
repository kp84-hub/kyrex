"""Observe the member portal's own request; never generate API credentials/calls."""
import hashlib
import json
import re
from urllib.parse import urlsplit

import booking_driver as base
import booking_schema as common
import workout_booking_schema as schema

CONTEXT = r'''e => {
 let n=e, text='';
 for(let i=0;n&&i<6;i++,n=n.parentElement){
   text=(n.innerText||'').trim();
   if(text.includes('Group Fitness Class')||n.getAttribute('role')==='dialog') break;
 }
 return text.slice(0,3000);
}'''


def observed(driver, allowed):
    snap = base.snapshot(driver, allowed)
    # Only the studio portal, never the gym's marketing copy, supplies prices.
    frames = [f for f in driver._page.frames if schema.portal_url(f.url)]
    if len(frames) != 1:
        raise ValueError('Open the Level 6 member portal in this Browser Host profile.')
    portal = frames[0]
    contexts = {}
    for c in snap['controls']:
        if c['frame'] != portal.url:
            continue
        locator = portal.locator(c['selector'])
        if locator.count() == 1:
            contexts[c['ref']] = locator.evaluate(CONTEXT)
    snap['contexts'] = contexts
    snap['signed_in'] = bool(re.search(r'\b(?:log\s*out|my bookings|my profile)\b',
                                      snap['frame_texts'].get(portal.url, ''), re.I))
    return snap


def terms(snapshot, control):
    context = snapshot['contexts'].get(control['ref'], '')
    if ('Group Fitness Class' not in context
            or re.search(r'wait\s*list|payment required|purchase|card number|deposit', context, re.I)
            or re.search(r'\$\s*(?!0(?:\.00)?(?:\D|$))\d', context)
            or not re.search(r'\bfree\b|\$\s*0(?:\.00)?\b|included (?:in|with) (?:your )?membership|\b[1-9]\d? credits?\b', context, re.I)):
        raise ValueError('The portal must show the class is included, free, or uses a stated credit before booking.')
    return re.search(r'\bfree\b|\$\s*0(?:\.00)?\b|included (?:in|with) (?:your )?membership|\b[1-9]\d? credits?\b', context, re.I)[0]


def identity(request):
    headers = request.all_headers()
    secret = headers.get('authorization') or headers.get('cookie')
    if not secret:
        raise ValueError('Sign in to your Glofox member account in the Browser Host manual viewer first.')
    return hashlib.sha256(secret.encode()).hexdigest()


def receipt(payload, spec):
    if not isinstance(payload, dict): return {'outcome': 'unknown'}
    booking = payload.get('Booking') if isinstance(payload, dict) else None
    details = spec['details']
    if (payload.get('success') is not True or not isinstance(booking, dict)
            or booking.get('branch_id') != schema.BRANCH
            or booking.get('event_id') != details['event_id']
            or booking.get('time_start') != details['start']
            or booking.get('status') != 'BOOKED' or booking.get('confirmed') is not True
            or booking.get('is_from_waiting_list') is True
            or not re.fullmatch(r'[a-f0-9]{24}', str(booking.get('_id', '')))):
        return {'outcome': 'unknown'}
    wanted = spec['request']['body'].get('user_id')
    if wanted and booking.get('user_id') != wanted:
        return {'outcome': 'unknown'}
    return {'outcome': 'confirmed', 'reference': booking['_id']}


def run(driver, proto, spec, *, allowlist, root):
    from browser_operator import domain_allowed
    spec = schema.task(spec)
    allowed = lambda url: domain_allowed(url, allowlist)[0]
    if not allowed(schema.URL):
        raise ValueError('Allow the Level 6 booking website in your Browser Bot and Host.')
    if not proto.operation('browser.booking', spec['id'], 'Inspect the owner-selected Level 6 class', ''):
        raise ValueError('The workout booking is not authorized.')
    if any(urlsplit(w.url).hostname == 'app.glofox.com' for w in driver._context.service_workers):
        raise ValueError('This portal profile has a service worker and needs a verified booking adapter.')
    capture = []
    responses = []
    blocked = []
    approved = [False]
    submitted = [False]
    before_click = [None]

    def guard(route):
        request = route.request
        if request.is_navigation_request() and not allowed(request.url):
            route.abort(); return
        if request.method in {'GET', 'HEAD', 'OPTIONS'}:
            if re.search(r'/(?:cancel|delete|checkout|purchase)(?:/|\?|$)', request.url, re.I):
                blocked.append('The portal attempted an unsupported update.'); route.abort(); return
            route.fallback(); return
        try:
            body = json.loads(request.post_data or '')
        except ValueError:
            body = None
        # Glofox's public guest bootstrap remains the only unapproved POST.
        if (request.url == 'https://api.glofox.com/2.0/login'
                and body == {'branch_id': schema.BRANCH, 'login': 'GUEST', 'password': 'GUEST'}):
            route.fallback(); return
        try:
            template = schema.request_template(request.url, request.method, body, identity(request))
            if not allowed(request.url) or body['model_id'] != spec['details']['event_id']:
                raise ValueError('The portal request targets a different workout.')
            if before_click[0] is None or not before_click[0][0]['signed_in']:
                raise ValueError('Sign in to your Glofox member account in the Browser Host manual viewer first.')
            if not re.search(r'\bbook\b|\bconfirm\b', before_click[0][1]['label'], re.I):
                raise ValueError('The portal tried to book during a date or class selection step.')
            reviewed_terms = terms(*before_click[0])
            snap, control = before_click[0]
            frames = [f for f in driver._page.frames if f.url == control['frame']]
            if len(frames) != 1: raise ValueError('The member portal changed.')
            live_context = frames[0].locator(control['selector']).evaluate(CONTEXT)
            live = {**snap, 'contexts': {control['ref']: live_context}}
            if terms(live, control) != reviewed_terms:
                raise ValueError('The membership or credit terms changed before booking.')
        except ValueError as exc:
            # Block telemetry writes too, without mistaking an analytics
            # request for the studio's native booking intent.
            parsed = urlsplit(request.url)
            if parsed.hostname in schema.HOSTS[2:] and re.search(r'book|login|pay|purchase', parsed.path, re.I):
                blocked.append(str(exc))
            route.abort(); return
        if spec['stage'] == 'prepare':
            capture.append(template); route.abort(); return
        if (not approved[0] or submitted[0] or template != spec['request']
                or not proto.operation('browser.booking', spec['id'], 'Submit the exact approved Level 6 class once', '')):
            blocked.append('The account, class or booking request changed.'); route.abort(); return
        # The persistent host claim happens before the ONE outgoing write.
        if not base.claim(driver.session_dir, spec['id']):
            blocked.append('This workout already had a booking attempt.'); route.abort(); return
        submitted[0] = True
        route.fallback()

    def response(response):
        if not submitted[0] or response.request.method != 'POST' or response.url != spec.get('request', {}).get('url'):
            return
        try:
            raw = response.body()
            if len(raw) <= 128 * 1024:
                payload = json.loads(raw)
                if isinstance(payload, dict): responses.append(payload)
        except Exception:
            pass

    driver._context.route('**/*', guard)
    driver._page.on('response', response)
    try:
        driver.navigate(schema.URL)
        # Follow only the studio's actual embedded Level 6 portal link.
        if not any(schema.portal_url(f.url) for f in driver._page.frames):
            urls = driver._page.locator('iframe[src]').evaluate_all('nodes=>nodes.map(n=>n.src)')
            portals = [u for u in urls if schema.portal_url(u) and allowed(u)]
            if len(portals) != 1:
                raise ValueError('The Level 6 member portal is unavailable or blocked by your site permissions.')
            driver.navigate(portals[0])
        driver._page.wait_for_timeout(700)
        if any(urlsplit(w.url).hostname == 'app.glofox.com' for w in driver._context.service_workers):
            raise ValueError('This portal uses a service worker and needs a verified booking adapter.')
        for index, step in enumerate(spec['steps']):
            if not proto.operation('browser.booking', spec['id'], 'Prepare the Level 6 member booking', ''):
                raise ValueError('Workout preparation was stopped.')
            snap = observed(driver, allowed)
            c = next((c for c in snap['controls'] if c['fingerprint'] == step['control']['fingerprint']), None)
            if not c or not schema.portal_url(c['frame']) or c['disabled']:
                raise ValueError('The workout control changed. Check fresh options.')
            locator = base.locate(driver, c, allowed)
            if step['op'] == 'click':
                if (re.search(r'cancel|delete|purchase|pay|wait\s*list|logout|log out', c['label'], re.I)
                        or not re.search(r'\b(?:book|confirm|class|classes|calendar|schedule|my bookings|next|previous|date|day|filter|show|select|available|view details)\b|^\d{1,2}(?:\D|$)', c['label'], re.I)):
                    raise ValueError('This is not a supported class selection control.')
                before_click[0] = (snap, c)
                locator.click(timeout=10000)
            else:
                if not common.field_allowed(step['field'], c) or c['type'] in {'password', 'file'}:
                    raise ValueError('Only requested class date/time fields can be entered.')
                getattr(locator, 'select_option' if step['op'] == 'select' else 'fill')(step['value'], timeout=10000)
            driver._page.wait_for_timeout(500)
            if capture:
                snap, target = before_click[0]
                return {'mode': schema.ACTION, 'status': 'no_changes', 'snapshot': snap,
                        'review': {'submit': target, 'request': capture[-1],
                                   'steps': spec['steps'][:index], 'terms': terms(snap, target)}}
        snap = observed(driver, allowed)
        if blocked:
            return {'mode': schema.ACTION, 'status': 'error', 'final_response': blocked[-1] + ' Nothing was booked.'}
        if not snap['signed_in'] or snap['blocked']:
            return {'mode': schema.ACTION, 'status': 'error',
                    'final_response': 'Sign in to your Glofox member account in the Browser Host manual viewer, close the viewer, then ask me again. Nothing was booked.'}
        if spec['stage'] == 'prepare':
            return {'mode': schema.ACTION, 'status': 'no_changes', 'snapshot': snap}
        if common.review_hash(snap) != spec['expected']:
            return {'mode': schema.ACTION, 'status': 'error', 'outcome': 'changed'}
        target = next((c for c in snap['controls'] if c['fingerprint'] == spec['submit']['fingerprint']), None)
        if not target or not re.search(r'\bbook\b|\bconfirm\b', target['label'], re.I):
            return {'mode': schema.ACTION, 'status': 'error', 'outcome': 'changed'}
        terms(snap, target)
        if not proto.operation('browser.booking', spec['id'], 'Recheck the approved workout before booking', ''):
            return {'mode': schema.ACTION, 'status': 'error', 'outcome': 'cancelled'}
        before_click[0] = (snap, target)
        approved[0] = True
        base.locate(driver, target, allowed).click(timeout=10000)
        for _ in range(12):
            driver._page.wait_for_timeout(500)
            if responses or blocked: break
        if responses:
            return {'mode': schema.ACTION, 'status': 'changes', **receipt(responses[-1], spec), 'url': snap['url']}
        return {'mode': schema.ACTION, 'status': 'error',
                'outcome': 'unknown' if submitted[0] else 'changed', 'url': snap['url']}
    except Exception:
        if submitted[0]:
            return {'mode': schema.ACTION, 'status': 'error', 'outcome': 'unknown'}
        raise
    finally:
        driver._page.remove_listener('response', response)
        driver._context.unroute('**/*', guard)
