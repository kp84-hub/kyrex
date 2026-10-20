"""Observe the member portal's own request; never generate API credentials/calls."""
import hashlib
import json
import re
import time
from decimal import Decimal
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


MEMBER_SESSION = r'''e => {
 const text = e.innerText || '';
 return {
   signed_in: /\b(log\s*out|my bookings|my profile)\b/i.test(text),
   signed_out: /\b(login\s*\/\s*register|log\s*in|sign\s*in)\b/i.test(text)
 };
}'''


# Author-declared click targets observed in the supported portal. Do not make
# arbitrary DIVs, Angular expressions, or directionless dropdown arrows usable.
CALENDAR_DOM = r'''() => {
 const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
 const day=e=>{
   const text=(e.innerText||'').trim().replace(/\s+/g,' ');
   return /^(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?) (?:[1-9]|[12][0-9]|3[01])$/i.test(text)
     ? text : '';
 };
 const inCalendar=e=>{
   let n=e.parentElement;
   for(let i=0;n&&i<4;i++,n=n.parentElement){
     if(n===document.body || n===document.documentElement) break;
     const days=[...n.querySelectorAll('li[ng-click],li[role=button]')].filter(x=>visible(x)&&day(x));
     if(days.length>=3 && days.length<=7) return true;
   }
   return false;
 };
 const labelFor=e=>{
   if(!e.hasAttribute('ng-click') && e.getAttribute('role')!=='button') return '';
   const declared=[e.getAttribute('ng-click'),e.getAttribute('onclick'),e.getAttribute('aria-label'),
     e.getAttribute('title'),e.innerText].filter(Boolean).join(' ')
     .replace(/([a-z])([A-Z])/g,'$1 $2');
   if(/cancel|delete|purchase|pay|checkout|wait\s*list|log\s*out|sign\s*in|family|account|membership|profile/i.test(declared)) return '';
   if(e.tagName==='LI') {
     const label=day(e);
     if(label) return label;
     if(/^classes$/i.test((e.innerText||'').trim())) return 'Classes';
   }
   if(e.tagName!=='DIV' || !inCalendar(e) ||
      e.querySelector('li[ng-click],div[ng-click],button,input,select,a[href],[role=button]')) return '';
   const icons=[...e.querySelectorAll('i,svg,span,img,use')].slice(0,12);
   const hints=[e,...icons].map(n=>[
     n.getAttribute('class'),n.getAttribute('aria-label'),n.getAttribute('title'),
     n===e?n.getAttribute('ng-click'):'',
     n.tagName==='IMG'?(n.getAttribute('src')||'').split(/[?#]/)[0].split('/').pop():'',
     n.tagName.toLowerCase()==='use'?(n.getAttribute('href')||'').split('#').pop():''
   ].filter(Boolean).join(' ')).join(' ').slice(0,1500)
     .replace(/([a-z])([A-Z])/g,'$1 $2').replace(/[_-]/g,' ');
   const next=/\b(?:next|right)\b/i.test(hints);
   const previous=/\b(?:prev|previous|left)\b/i.test(hints);
   if(next===previous) return '';
   return next?'Next calendar week':'Previous calendar week';
 };
 return (''' + base.DOM + r''')({
   extraSelector:'li[ng-click],div[ng-click]', extraFilter:e=>!!labelFor(e), labelFor,
   disabledFor:e=>e.matches('.disabled,[disabled]')
 });
}'''


DAY_CONTROL = re.compile(r'^(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\s+(?:[1-9]|[12][0-9]|3[01])$', re.I)


def member_session(page, allowed, *, timeout_ms=12000):
    """Observe the supported portal's async session UI; return fixed states.

    No storage, cookie, token or account values leave the page. An initial
    login link can be replaced when the saved session finishes loading, so
    only positive member evidence ends the wait early. Loading, detachment,
    ambiguity and missing UI are never treated as proof of logout.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    state = 'unsupported_portal'
    while True:
        frames = [f for f in page.frames if schema.portal_url(f.url) and allowed(f.url)]
        if len(frames) == 1:
            try:
                flags = frames[0].locator('body').evaluate(MEMBER_SESSION, timeout=500)
                if flags.get('signed_in') and not flags.get('signed_out'):
                    return 'signed_in'
                state = ('signed_out' if flags.get('signed_out') and not flags.get('signed_in')
                         else 'session_unverified')
            except Exception:
                state = 'session_unverified'
        else:
            state = 'unsupported_portal'
        remaining = (deadline - time.monotonic()) * 1000
        if remaining <= 0:
            return state
        page.wait_for_timeout(min(250, remaining))


def observed(driver, allowed):
    snap = base.snapshot(driver, allowed, dom_for_frame=lambda url:
                         CALENDAR_DOM if schema.portal_url(url) else base.DOM)
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
    amounts = re.findall(r'(?:[$€£]|\bUSD\s*|\bEUR\s*|\bGBP\s*)\s*(\d+(?:[.,]\d{1,2})?)', context, re.I)
    proof = re.search(r'(?mi)^\s*free\s*$|\b(?:class|booking|price|cost)\s*:?\s*free(?=\s*[.!]?\s*(?:\n|$))|\$\s*0(?:\.00)?(?![\d.,])\b|included (?:in|with) (?:your )?membership|\b[1-9]\d? credits?\b', context)
    if ('Group Fitness Class' not in context
            or re.search(r'wait\s*list|payment required|purchase|card number|deposit|not included|insufficient credits|no active membership', context, re.I)
            or any(Decimal(amount.replace(',', '.')) != 0 for amount in amounts)
            or not proof):
        raise ValueError('The portal must show the class is included, free, or uses a stated credit before booking.')
    return proof[0]


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
    spec = schema.task(spec)
    result = _run(driver, proto, spec, allowlist=allowlist, root=root)
    if result.get('outcome') != 'unknown':
        return result
    # _run returns unknown only after the durable claim and outgoing write.
    # Its write guard/listener have been removed before this separate read.
    # Never replay the submit or infer success from a click/toast/empty list.
    try:
        from datetime import date, timedelta
        import member_bookings_driver
        window = {'url': schema.URL, 'start': spec['details']['date'],
                  'end': (date.fromisoformat(spec['details']['date']) + timedelta(days=1)).isoformat()}
        evidence = member_bookings_driver.run(driver, proto, window, allowlist=allowlist,
                                              expected_request=spec['request'])
        matches = [row for row in evidence['bookings']
                   if row['event_id'] == spec['details']['event_id']
                   and row['start'] == spec['details']['start']
                   and row['title'] == spec['details']['class_name']]
        if evidence['complete'] is True and len(matches) == 1:
            return {'mode': schema.ACTION, 'status': 'changes', 'outcome': 'confirmed',
                    'reference': matches[0]['reference'], 'url': schema.URL}
    except Exception:
        pass  # Lost browser, denied read, changed account, unsupported data: still unknown.
    return result


def _run(driver, proto, spec, *, allowlist, root):
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
    submitted_request = [None]
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
        if schema.guest_bootstrap(request.url, request.method, body):
            route.fallback(); return
        try:
            template = schema.request_template(request.url, request.method, body, identity(request))
            if (not allowed(request.url) or not allowed(template['url'])
                    or body['model_id'] != spec['details']['event_id']):
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
        submitted_request[0] = request
        if 'native' in template:
            # Only after exact source + destination review and the durable
            # claim. Same origin and saved member headers; never send 2.0.
            route.fallback(url=template['url'], post_data=json.dumps(template['body']))
        else:
            route.fallback()

    def response(response):
        # A routed URL override can retain the original URL in the browser's
        # response event. Correlate the exact claimed request, not a URL guess.
        if not submitted[0] or response.request != submitted_request[0]:
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
    phase, index = 'navigation', None
    try:
        driver.navigate(schema.URL)
        phase = 'member_session'
        session = member_session(driver._page, allowed)
        # Follow only the studio's actual embedded Level 6 portal link.
        if session == 'unsupported_portal' and not any(schema.portal_url(f.url) for f in driver._page.frames):
            urls = driver._page.locator('iframe[src]').evaluate_all('nodes=>nodes.map(n=>n.src)')
            portals = [u for u in urls if schema.portal_url(u) and allowed(u)]
            if len(portals) != 1:
                raise ValueError('The Level 6 member portal is unavailable or blocked by your site permissions.')
            phase = 'navigation'
            driver.navigate(portals[0])
            phase = 'member_session'
            session = member_session(driver._page, allowed)
        if any(urlsplit(w.url).hostname == 'app.glofox.com' for w in driver._context.service_workers):
            raise ValueError('This portal uses a service worker and needs a verified booking adapter.')
        if session != 'signed_in':
            message = ('Sign in to your Glofox member account in the Browser Host manual viewer, close the viewer, then ask me again.'
                       if session == 'signed_out' else
                       'The Glofox member session could not be verified. Check the existing session in the private viewer.')
            return {'mode': schema.ACTION, 'status': 'error',
                    'final_response': message + ' Nothing was booked.'}
        for index, step in enumerate(spec['steps']):
            if not proto.operation('browser.booking', spec['id'], 'Prepare the Level 6 member booking', ''):
                raise ValueError('Workout preparation was stopped.')
            phase = 'snapshot'
            snap = observed(driver, allowed)
            phase = 'replay'
            c = next((c for c in snap['controls'] if c['fingerprint'] == step['control']['fingerprint']), None)
            if not c or not schema.portal_url(c['frame']) or c['disabled']:
                raise ValueError('The workout control changed. Check fresh options.')
            locator = base.locate(driver, c, allowed)
            if step['op'] == 'click':
                if (re.search(r'cancel|delete|purchase|pay|wait\s*list|logout|log out', c['label'], re.I)
                        or not (DAY_CONTROL.fullmatch(c['label']) or re.search(r'\b(?:book|confirm|class|classes|calendar|schedule|my bookings|next|previous|date|day|filter|show|select|available|view details)\b|^\d{1,2}(?:\D|$)', c['label'], re.I))):
                    raise ValueError('This is not a supported class selection control.')
                before_click[0] = (snap, c)
                locator.click(timeout=10000)
            else:
                if not common.field_allowed(step['field'], c) or c['type'] in {'password', 'file'}:
                    raise ValueError('Only requested class date/time fields can be entered.')
                getattr(locator, 'select_option' if step['op'] == 'select' else 'fill')(step['value'], timeout=10000)
            driver._page.wait_for_timeout(500)
            if capture:
                phase = 'review'
                snap, target = before_click[0]
                return {'mode': schema.ACTION, 'status': 'no_changes', 'snapshot': snap,
                        'review': {'submit': target, 'request': capture[-1],
                                   'steps': spec['steps'][:index], 'terms': terms(snap, target)}}
        phase = 'snapshot'
        snap = observed(driver, allowed)
        phase = 'review'
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
        phase = 'submission'
        base.locate(driver, target, allowed).click(timeout=10000)
        phase = 'receipt'
        for _ in range(12):
            driver._page.wait_for_timeout(500)
            if responses or blocked: break
        if responses:
            return {'mode': schema.ACTION, 'status': 'changes', **receipt(responses[-1], spec), 'url': snap['url']}
        return {'mode': schema.ACTION, 'status': 'error',
                'outcome': 'unknown' if submitted[0] else 'changed', 'url': snap['url']}
    except Exception as exc:
        exc.kyrex_workout_phase = phase
        exc.kyrex_workout_step = index
        if submitted[0]:
            failure = schema.log_browser_failure(exc)
            return {'mode': schema.ACTION, 'status': 'error', 'outcome': 'unknown',
                    'workout_failure': failure}
        raise
    finally:
        try: driver._page.remove_listener('response', response)
        except Exception: pass
        try: driver._context.unroute('**/*', guard)
        except Exception: pass
