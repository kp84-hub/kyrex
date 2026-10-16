"""Owner-run, write-blocked portal request capture; never grants booking authority.

Run inside the existing Browser Host container through stdin. No image rebuild,
model, Cloud submission, generated API call, or profile reset is required.
"""
import argparse
import json
import os
import re
import signal
import time
from datetime import datetime
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
BOOK_LABEL = re.compile(r'book(?: (?:now|class|a class|this class))?|confirm(?: (?:booking|class))?', re.I)


def time_pattern(clock):
    hour, minute = map(int, clock.split(':'))
    return re.compile(r'(?<![\d:])0?' + str(hour % 12 or 12) + r'\s*:\s*' + f'{minute:02d}'
                      + r'\s*' + ('p' if hour >= 12 else 'a') + r'\s*\.?\s*m\b\.?', re.I)


def control_evidence(snapshot, clock):
    """Expose individual failed predicates, never labels, text or selectors."""
    wanted_time = time_pattern(clock)
    checks = []
    portal_text = '\n'.join(value for url, value in snapshot.get('frame_texts', {}).items() if schema.portal_url(url))
    for control in snapshot['controls']:
        if not schema.portal_url(control['frame']): continue
        label = ' '.join(control['label'].split())
        context = snapshot['contexts'].get(control['ref'], '')
        book_word = bool(re.search(r'\bbook\b|\bconfirm\b', label, re.I))
        class_in_context = 'Group Fitness Class' in context
        if not book_word and not class_in_context: continue
        checks.append({'tag': control['tag'] if control['tag'] in {'button', 'a', 'li', 'div', 'input', 'select'} else 'other',
                       'book_or_confirm_word': book_word, 'supported_label': bool(BOOK_LABEL.fullmatch(label)),
                       'disabled': bool(control['disabled']), 'class_in_context': class_in_context,
                       'requested_time_in_context': bool(wanted_time.search(context)),
                       'requested_time_in_label': bool(wanted_time.search(label))})
    return {'portal_control_count': sum(bool(schema.portal_url(c['frame'])) for c in snapshot['controls']),
            'class_in_page': 'Group Fitness Class' in portal_text,
            'requested_time_in_page': bool(wanted_time.search(portal_text)),
            'unsigned_agreements_notice': bool(re.search(r'unsigned e.agreements', portal_text, re.I)),
            'past_event_notice': bool(re.search(r'\bevent passed\b', portal_text, re.I)),
            'control_checks': checks[:30]}


CONTROL_SUMMARY = r'''pattern => {
 const time=new RegExp(pattern,'i');
 const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
 const nodes=[...document.querySelectorAll('button,a[href],input,[role=button],[ng-click]')]
   .filter(visible).slice(0,500);
 const matches=nodes.filter(e=>/\bbook\b|\bconfirm\b/i.test(e.innerText||e.getAttribute('aria-label')||''));
 return {visible_controls:nodes.length, book_or_confirm_controls:matches.length,
   controls:matches.slice(0,20).map(e=>{
     let n=e, depth=null;
     for(let i=0;n&&i<8&&n!==document.body&&n!==document.documentElement;i++,n=n.parentElement){
       const text=n.innerText||'';
       if(text.includes('Group Fitness Class')&&time.test(text)){depth=i;break;}
     }
     return {tag:['BUTTON','A','INPUT','DIV','LI'].includes(e.tagName)?e.tagName.toLowerCase():'other',
       angular_click:e.hasAttribute('ng-click'), role_button:e.getAttribute('role')==='button',
       disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true',
       class_and_requested_time_ancestor_depth:depth};
   })};
}'''


CLASS_ACTIONS = r'''options => {
 const time=new RegExp(options.time,'i'), label=new RegExp('^(?:'+options.label+')$','i');
 const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden'
   && !e.closest('[aria-hidden=true],[inert]');
 const text=e=>(e.innerText||e.getAttribute('aria-label')||'').trim().replace(/\s+/g,' ');
 const dialogs=[...document.querySelectorAll('[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open]')].filter(visible);
 const active=dialogs.filter(e=>!dialogs.some(other=>other!==e&&e.contains(other)));
 const scope=active.length===1?active[0]:null;
 const visibleNodes=[...document.querySelectorAll('button,a[href],input,[role=button],[ng-click]')].filter(visible);
 const all=scope?visibleNodes.filter(e=>scope.contains(e)):visibleNodes;
 const nodes=all.slice(0,500).filter(e=>label.test(text(e)));
 const roots=nodes.filter(e=>!nodes.some(other=>other!==e&&other.contains(e)));
 const observed=options.controls.map(c=>{
   const matches=document.querySelectorAll(c.selector.replace(/^css=/,''));
   return {ref:c.ref,eligible:c.eligible,node:matches.length===1?matches[0]:null};
 });
 const groups=roots.map(root=>{
   const members=nodes.filter(e=>root.contains(e));
   const leaves=members.filter(e=>!members.some(other=>other!==e&&e.contains(other)));
   const disabled=members.some(e=>!!e.closest('[disabled],[aria-disabled=true],.disabled,.md-disabled'));
   let rowClass=false,rowTime=false,depth=null,multiple=false;
   for(let n=root,i=0;n&&i<8&&n!==document.body&&n!==document.documentElement&&(!scope||scope.contains(n));i++,n=n.parentElement){
     // A parent containing another disjoint booking action is a list/group,
     // never evidence that this control belongs to a neighbouring class.
     if(roots.filter(other=>n.contains(other)).length!==1){multiple=true;break;}
     const content=n.innerText||'';
     rowClass=rowClass||content.includes('Group Fitness Class');
     rowTime=rowTime||time.test(content);
     if(content.includes('Group Fitness Class')&&time.test(content)){depth=i;break;}
   }
   // Collapse one nested chain, not two independent actions or two classes.
   const chosen=members.map(node=>observed.find(c=>c.node===node)).find(Boolean);
   return {ref:chosen?.ref||null,eligible_for_step:!!chosen&&(!!scope||!options.confirmation||chosen.eligible),
     disabled,branched:leaves.length!==1,
     class_in_row:rowClass,requested_time_in_row:rowTime,context_depth:depth,
     stopped_at_other_action:multiple,nested_control_count:members.length};
 });
 const refs=all.length>500||active.length>1?[]:groups.filter(g=>g.ref&&g.eligible_for_step&&!g.disabled&&!g.branched&&g.context_depth!==null).map(g=>g.ref);
 return {refs,book_control_count:nodes.length,action_group_count:groups.length,
   visible_dialog_count:dialogs.length,dialog_scope_count:active.length,dialog_scoped:!!scope,
   new_controls_only:!!options.confirmation&&!scope,ambiguous_dialogs:active.length>1,
   scan_truncated:all.length>500,groups:groups.slice(0,30).map(({ref,...flags})=>flags)};
}'''


def class_actions(page, snapshot, allowed, clock, *, previous_controls=None):
    portals = [frame for frame in page.frames if schema.portal_url(frame.url) and allowed(frame.url)]
    if len(portals) != 1: return [], {'status': 'portal_not_unique'}
    controls = [c for c in snapshot['controls'] if c['frame'] == portals[0].url
                and BOOK_LABEL.fullmatch(' '.join(c['label'].split()))]
    groups = portals[0].evaluate(CLASS_ACTIONS, {'time': time_pattern(clock).pattern,
        'label': BOOK_LABEL.pattern, 'confirmation': previous_controls is not None,
        'controls': [{'ref': c['ref'], 'selector': c['selector'],
                      'eligible': previous_controls is None or c['fingerprint'] not in previous_controls} for c in controls]})
    refs = set(groups.pop('refs'))
    candidates = [common.control(c) for c in controls if c['ref'] in refs and not c['disabled']]
    return candidates, groups


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


def inspect(driver, allowlist, day, clock, *, progress=None, control_wait=6):
    """Observe only an exact visible day/time. All member writes are aborted."""
    common.details({'date': day, 'time': clock})
    allowed = lambda url: host_allowlist.domain_allowed(url, allowlist)[0]
    if not allowed(schema.URL): return {'status': 'site_permissions'}
    context, page = driver._context, driver._page
    if context.service_workers: return {'status': 'service_worker_present'}
    evidence, phase = [], ['opening']
    emit = progress or (lambda stage: None)

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
    emit('opening_portal')
    driver.navigate(schema.URL)
    if workout.member_session(page, allowed) != 'signed_in':
        return {'status': 'member_session_unverified', 'requests': evidence}
    emit('member_session_verified')
    snap = workout.observed(driver, allowed)
    # Keep this stdin probe compatible with the installed host: the shared
    # requested-day helper shipped in a later Cloud-only update.
    wanted = datetime.strptime(day, '%Y-%m-%d').date()
    weekday = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')[wanted.weekday()]
    candidates = []
    for control in snap['controls']:
        label = ' '.join(control['label'].split())
        match = re.fullmatch(r'([A-Za-z]+) ([1-9]|[12][0-9]|3[01])', label)
        if (schema.portal_url(control['frame']) and workout.DAY_CONTROL.fullmatch(label)
                and match and match[1][:3].lower() == weekday and int(match[2]) == wanted.day):
            candidates.append(control)
    if len(candidates) != 1 or candidates[0]['disabled']:
        return {'status': 'requested_day_not_unique_or_visible', 'requests': evidence}
    day_control = common.control(candidates[0])
    phase[0] = 'calendar'
    workout.base.locate(driver, day_control, allowed).click(timeout=10000)
    emit('requested_day_selected')
    page.wait_for_timeout(1000)
    time_label = time_pattern(clock)
    previous_controls = None
    for step_index in range(2):
        deadline = time.monotonic() + control_wait
        while True:
            snap = workout.observed(driver, allowed)
            candidates, groups = class_actions(page, snap, allowed, clock, previous_controls=previous_controls)
            if len(candidates) == 1 or time.monotonic() >= deadline: break
            page.wait_for_timeout(250)
        if len(candidates) != 1:
            detail = control_evidence(snap, clock)
            detail['matching_control_count'] = len(candidates)
            detail['action_groups'] = groups
            portals = [frame for frame in page.frames if schema.portal_url(frame.url) and allowed(frame.url)]
            if len(portals) == 1:
                try: detail['dom'] = portals[0].evaluate(CONTROL_SUMMARY, time_label.pattern)
                except Exception: detail['dom'] = {'status': 'unavailable'}
            return {'status': 'class_control_not_unique_or_visible', 'requests': evidence, 'controls': detail}
        phase[0] = 'class' if step_index == 0 else 'confirmation'
        emit('inspecting_class_control' if step_index == 0 else 'inspecting_confirmation_control')
        previous_controls = {c['fingerprint'] for c in snap['controls'] if schema.portal_url(c['frame'])}
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
            def progress(stage): print(json.dumps({'stage': stage}), flush=True)
            progress('starting_browser')
            driver.open()
            result = inspect(driver, allowlist, args.date, args.time, progress=progress)
        except Exception as exc:
            result = {'status': 'capture_failed', 'failure': schema.browser_failure(exc)}
        finally:
            driver.close()
            signal.alarm(0)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
