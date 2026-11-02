"""Observe the portal's own cancellation dialog and intercept its exact DELETE."""
import json
import re
import time
from urllib.parse import urlsplit

import booking_driver as base
import member_bookings_driver as reader
import member_bookings_schema as member
import workout_booking_driver as workout_driver
import workout_booking_schema as workout
import workout_cancellation_schema as schema


def run(driver, proto, spec, *, allowlist, root):
    from browser_operator import domain_allowed
    spec = schema.task(spec)
    context, page = driver._context, driver._page
    members, captured, responses = [], [], []
    submitted = [False]
    attempted = [False]
    outgoing = [None]
    armed = [False]
    guards = []
    read_guards = []
    read_replaced = False
    allowed = lambda url: domain_allowed(url, allowlist)[0] and domain_allowed(url, list(member.READER_HOSTS))[0]

    def identify(route):
        req = route.request
        ident = member.native_member(req.url) if req.method == 'GET' else None
        if ident and allowed(req.url): members.append(ident)
        route.fallback()

    context.route('**/*', identify); guards.append(identify)
    try:
        if not proto.operation('browser.booking', spec['id'], 'Inspect the existing reservation for cancellation', ''):
            raise ValueError('Cancellation preparation was stopped.')
        found = reader.run(driver, proto, spec['window'], allowlist=allowlist, guard_sink=read_guards)
        if (found.get('identity') != spec['identity'] or spec['details'] not in found.get('bookings', [])
                or len(set(members)) != 1):
            return {'mode': workout.ACTION, 'status': 'error', 'outcome': 'changed',
                    'final_response': 'The booked reservation or member session changed. Nothing was cancelled.'}
        if spec['details']['start'] <= time.time():
            raise ValueError('This class has already started. Cancellation was not submitted.')
        frames = [f for f in page.frames if workout.portal_url(f.url) and allowed(f.url)]
        if len(frames) != 1: raise ValueError('The member portal could not be verified.')
        portal = frames[0]

        def guard(route):
            req = route.request
            if (not allowed(req.url)
                    or re.search(r'[?&](?:action|op|method)=(?:cancel|delete|submit|purchase|logout)(?:&|$)', req.url, re.I)
                    or re.search(r'/(?:cancel|delete|logout|signout|checkout|purchase|submit|confirm|attendances)(?:/|$)',
                                 urlsplit(req.url).path, re.I)):
                route.abort(); return
            if req.method in {'GET', 'HEAD', 'OPTIONS'}:
                route.fallback(); return
            if reader.session_refresh(req):
                route.fallback(); return
            try:
                if not workout.portal_url(req.frame.url): raise ValueError('Cancellation left the portal frame.')
                secret = req.all_headers().get('authorization', '')
                if len(set(members)) != 1 or not secret or member.session_identity(secret, members[0]) != spec['identity']:
                    raise ValueError('Member identity changed.')
                native = schema.request(req.url, req.method, req.post_data, spec['identity'], spec['details']['reference'])
                if not armed[0] or req.redirected_from is not None:
                    raise ValueError('Cancellation was not armed by the reviewed control.')
                if spec['stage'] == 'prepare':
                    captured.append(native); route.abort(); return
                if (native != spec['request'] or submitted[0]
                        or not proto.operation('browser.booking', spec['id'], 'Cancel the exact approved reservation once', '')):
                    raise ValueError('The cancellation authority changed.')
                if not base.claim(driver.session_dir, spec['id']):
                    attempted[0] = True
                    raise ValueError('This cancellation already had an attempt.')
                submitted[0] = True; outgoing[0] = req
                route.fallback()
            except (ValueError, TypeError, KeyError):
                route.abort()

        def receipt(response):
            if outgoing[0] == response.request:
                try:
                    raw = response.body()
                    if len(raw) > 128 * 1024: return
                    payload = json.loads(raw)
                    if response.status == 200 and isinstance(payload, dict) and payload.get('success') is True:
                        responses.append(True)
                except Exception:
                    pass

        context.route('**/*', guard); guards.append(guard)
        # Install the cancellation guard before replacing the reader guard.
        # There is no unguarded interval; all other writes remain denied.
        for read_guard in read_guards: context.unroute('**/*', read_guard)
        read_replaced = True
        page.on('response', receipt)
        try:
            target = portal.locator('#profile-booking-' + spec['details']['reference'] + '-cancel')
            if target.count() != 1 or not target.is_visible():
                # The schedule can prefetch bookings without opening Profile.
                # Click only observed self-profile/booking controls; never a
                # family-account selector or a model-generated script.
                profile = portal.locator('#mobile-header-profile-button, #header-profile-menu-item').filter(visible=True)
                if profile.count() == 0:
                    profile = portal.get_by_text(re.compile(r'^(?:Profile|My profile)$', re.I)).filter(visible=True)
                if profile.count() != 1: raise ValueError('The self-profile control is unavailable or ambiguous.')
                profile.click(timeout=3000)
                portal.locator('#profile-modal').wait_for(state='visible', timeout=5000)
                tab = portal.locator('#profile-my-bookings-tab').filter(visible=True)
                if tab.count() == 1: tab.click(timeout=3000)
                target.wait_for(state='visible', timeout=5000)
            if target.count() != 1 or not target.is_visible() or not target.is_enabled():
                raise ValueError('The existing reservation has no supported cancellation control.')
            target.click(timeout=3000)
            button = portal.locator('#general-dialog-confirm-button')
            button.wait_for(state='visible', timeout=5000)
            if button.count() != 1 or not button.is_enabled() or button.inner_text().strip() != 'Cancel booking':
                raise ValueError('The portal does not allow cancellation of this reservation.')
            snap = workout_driver.observed(driver, allowed)
            controls = [c for c in snap['controls'] if c['selector'] == 'css=#general-dialog-confirm-button'
                        and c['frame'] == portal.url]
            if len(controls) != 1: raise ValueError('The cancellation confirmation is ambiguous.')
            control = controls[0]
            # Read the observed confirmation dialog only, not the whole profile.
            dialog = button.locator('xpath=ancestor::*[@role="dialog"][1]')
            if dialog.count() != 1: raise ValueError('The cancellation confirmation dialog is unsupported.')
            text = schema.terms(dialog.inner_text())
            native = schema.request('https://api.glofox.com/2.0/bookings/' + spec['details']['reference'],
                                    'DELETE', None, spec['identity'], spec['details']['reference'])
            expected = schema.digest(spec, control, text, native)
            if spec['stage'] == 'submit' and expected != spec['expected']:
                return {'mode': workout.ACTION, 'status': 'error', 'outcome': 'changed'}
            # Re-read the exact terms immediately before the native click.
            if schema.terms(dialog.inner_text()) != text:
                raise ValueError('Cancellation terms changed.')
            armed[0] = True
            button.click(timeout=3000)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and not (captured or responses): page.wait_for_timeout(100)
            if spec['stage'] == 'prepare':
                if captured != [native]: raise ValueError('The portal did not expose the expected cancellation request.')
                return {'mode': workout.ACTION, 'status': 'no_changes', 'snapshot': snap,
                        'review': {'submit': control, 'request': native, 'terms': text, 'expected': expected}}
            return {'mode': workout.ACTION, 'status': 'changes' if responses else 'error',
                    'outcome': 'confirmed' if responses else 'unknown' if submitted[0] or attempted[0] else 'changed',
                    'reference': spec['details']['reference'] if responses else ''}
        finally:
            page.remove_listener('response', receipt)
    except Exception:
        if submitted[0] or attempted[0]:
            return {'mode': workout.ACTION, 'status': 'error', 'outcome': 'unknown'}
        raise
    finally:
        # Teardown/unload cannot become a second write after the return value.
        if read_replaced:
            for read_guard in read_guards: context.route('**/*', read_guard)
        for guard in guards: context.unroute('**/*', guard)
