"""Observed-control booking preparation/replay and one owner-approved submit."""
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit, unquote

import booking_schema as schema

DOM = r'''(options = {}) => {
 options=options||{};
 const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
 const selector = e => {
   if(e.id && document.querySelectorAll('#'+CSS.escape(e.id)).length===1) return 'css=#'+CSS.escape(e.id);
   let parts=[];
   for(let n=e;n&&n.nodeType===1&&parts.length<12;n=n.parentElement) {
     let part=n.tagName.toLowerCase();
     const same=n.parentElement ? [...n.parentElement.children].filter(x=>x.tagName===n.tagName) : [];
     if(same.length>1) part+=':nth-of-type('+(same.indexOf(n)+1)+')';
     parts.unshift(part);
   }
   return 'css='+parts.join(' > ');
 };
 const native=[...document.querySelectorAll('input:not([type=hidden]),select,textarea,button,a[href],[role=button]')];
 const extra=options.extraSelector ? [...document.querySelectorAll(options.extraSelector)]
   .filter(e=>!options.extraFilter || options.extraFilter(e)) : [];
 const nodes=[...new Set([...native,...extra])].filter(visible).slice(0,120);
 return {text:(document.body?.innerText||'').slice(0,10000), headings:[...document.querySelectorAll('h1,h2,h3,[role=status]')].filter(visible).slice(0,30).map(e=>e.innerText.trim().slice(0,200)), controls:nodes.map(e=>({
   selector:selector(e), label:(options.labelFor?.(e)||e.getAttribute('aria-label')||e.labels?.[0]?.innerText||e.innerText||e.placeholder||e.name||'').trim().slice(0,200),
   tag:e.tagName.toLowerCase(),type:(e.getAttribute('type')||'').toLowerCase(),value:(/name|email|e-mail|phone|mobile|tel|date|time|party|people|guests|location|city|search/i.test(e.getAttribute('aria-label')||e.labels?.[0]?.innerText||e.placeholder||e.name||'') && !/password|token|secret|credential|birth|expir|credit|card/i.test(e.getAttribute('aria-label')||e.labels?.[0]?.innerText||e.placeholder||e.name||'') ? e.value||'' : '').slice(0,200),
   href:e.tagName==='A'?e.href:'', required:!!e.required,disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true'||!!options.disabledFor?.(e),
   form_action:e.form?.action||'',form_method:e.form?.method||'',
   options:e.tagName==='SELECT'?[...e.options].slice(0,100).map(o=>({value:o.value,label:o.text,disabled:o.disabled})):[]
 }))};
}'''


def snapshot(driver, allowed, *, dom_for_frame=None):
    page = driver._page
    if not allowed(page.url): raise ValueError('Booking page left the permitted sites.')
    records, texts, frame_texts, headings = [], [], {}, []
    blocked = False
    for frame in list(page.frames)[:5]:
        if not frame.url or frame.url == 'about:blank': continue
        if not allowed(frame.url): continue
        data = frame.evaluate(dom_for_frame(frame.url) if dom_for_frame else DOM)
        texts.append(data['text']); frame_texts[frame.url]=data['text']; headings.extend(data.get('headings',[]))
        blocked = blocked or any(c['type']=='password' for c in data['controls'])
        for item in data['controls']:
            if item['type'] in {'password','file','hidden'}: continue
            item['frame'] = frame.url
            item['fingerprint'] = schema.fingerprint(item)
            item['ref'] = 'c' + str(len(records))
            records.append(item)
    return {'url': page.url, 'title': page.title()[:200], 'text':'\n'.join(texts)[:12000], 'controls':records[:120], 'headings':headings[:30], 'frame_texts':frame_texts, 'blocked':blocked or bool(re.search(r'captcha|verify you are human', '\n'.join(texts), re.I))}


def locate(driver, item, allowed):
    frames = [f for f in driver._page.frames if f.url == item['frame'] and allowed(f.url)]
    if len(frames) != 1: raise ValueError('The booking frame changed. Review again.')
    locator = frames[0].locator(item['selector'])
    if locator.count() != 1 or not locator.is_visible() or not locator.is_enabled():
        raise ValueError('The booking control changed or is unavailable.')
    return locator


def step(driver, item, details, allowed):
    current = snapshot(driver, allowed)
    observed = next((c for c in current['controls'] if c['fingerprint'] == item['control']['fingerprint']), None)
    if observed is None or observed['disabled']: raise ValueError('The booking page changed during preparation.')
    label = observed['label']
    if schema.FORBIDDEN.search(label): raise ValueError('This booking step requires unsupported account or payment access.')
    if observed.get('form_action') and not allowed(observed['form_action']): raise ValueError('The booking form sends data outside the permitted sites.')
    locator = locate(driver, observed, allowed)
    if item['op'] in {'fill','select'}:
        if item['field'] in {'name','first_name','last_name','phone','email'} and not schema.business_matches(current,details):
            raise ValueError('The business or location could not be verified before entering contact details.')
        if not schema.matches(item['field'], item['value'], schema.field_value(details,item['field'])): raise ValueError('The field does not match your booking details.')
        if not schema.field_allowed(item['field'],observed): raise ValueError('This field is not the requested booking contact or date field.')
        if observed['tag'] not in {'input','select','textarea'} or observed['type'] in {'submit','button','checkbox','radio','password','file'}:
            raise ValueError('This is not a supported booking input.')
        if item['op'] == 'select':
            option = next((o for o in observed['options'] if o['value']==item['value'] and not o['disabled']),None)
            if option is None: raise ValueError('The requested booking option is unavailable.')
            locator.select_option(item['value'], timeout=10000)
        else: locator.fill(item['value'], timeout=10000)
    elif item['op'] == 'link':
        # Only observed reservation links; no invented URLs or private destinations.
        if not re.search(r'\b(?:book|reservation|reserve|appointment|availability|table)\b', label, re.I): raise ValueError('This link is not a booking link.')
        if not allowed(observed['href']): raise ValueError('The booking provider is outside your browser sites.')
        driver.navigate(observed['href'])
    else:
        if schema.COMMIT_LABEL.search(label) or not schema.PREPARE_LABEL.fullmatch(label):
            raise ValueError('This control requires final booking approval; it cannot be clicked while preparing.')
        locator.click(timeout=10000)
    driver._page.wait_for_timeout(300)
    if not allowed(driver._page.url): raise ValueError('Booking navigation left the permitted sites.')


def claim(profile, booking_id):
    path = Path(profile) / 'kyrex-booking-attempts.sqlite3'
    with sqlite3.connect(path, timeout=10) as db:
        db.execute('CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, created REAL NOT NULL)')
        try: db.execute('INSERT INTO attempts VALUES (?,?)', (booking_id,time.time()))
        except sqlite3.IntegrityError: return False
    path.chmod(0o600)
    return True


def receipt(snapshot, details):
    text = snapshot['text']
    ack=any(re.fullmatch(r"(?:your )?(?:reservation|booking|appointment) (?:is |has been )?confirmed[!.]?|you(?:’|')?re booked[!.]?",h.strip(),re.I) for h in snapshot.get('headings',[]))
    if ack and re.search(r'\b(?:reservation|booking|appointment)\s+(?:is\s+|has been\s+)?confirmed\b|\byou(?:’|\')?re booked\b', text, re.I):
        # A success phrase alone is not proof that this is the requested booking.
        wanted = str(details['venue']).casefold()
        normalized = re.sub(r'[^a-z0-9]', '', text.casefold())
        venue = re.sub(r'[^a-z0-9]', '', wanted)
        date_ok = details['date'] in text
        from datetime import date
        d=date.fromisoformat(details['date'])
        date_ok = date_ok or any(v in text for v in (d.strftime('%B %-d, %Y'), d.strftime('%b %-d, %Y'),d.strftime('%m/%d/%Y')))
        time_ok = any(schema.matches('time', m, details['time']) for m in re.findall(r'\b\d{1,2}:\d{2}(?:\s*[APap][Mm])?\b',text))
        party_ok=bool(re.search(r'(?:party(?:\s+(?:size|of))?\s*[: ]\s*'+str(details['party_size'])+r'\b|\b'+str(details['party_size'])+r'\s+(?:people|guests|persons)\b)',text,re.I))
        name_ok=details['name'].casefold() in re.sub(r'\s+',' ',text).casefold()
        if venue and venue in normalized and date_ok and time_ok and party_ok and name_ok:
            ref = re.search(r'(?:confirmation|reservation|booking)\s*(?:number|code|reference|#)\s*[:#]?\s*([A-Za-z0-9-]{3,60})',text,re.I)
            reference=ref[1] if ref and (any(c.isdigit() for c in ref[1]) or '-' in ref[1] or ref[1].isupper() and len(ref[1])>=5) else ''
            return {'outcome':'confirmed','reference':reference, 'url':snapshot['url'],'evidence':text[:3000]}
    if re.search(r'\b(?:request (?:received|submitted)|we(?:’|\')?ve received your request)\b', text, re.I):
        return {'outcome':'submitted','reference':'','url':snapshot['url'],'evidence':text[:3000]}
    return {'outcome':'unknown','reference':'','url':snapshot['url'],'evidence':text[:3000]}


ATOMIC_SUBMIT = """(element, expected) => {
    const state = (""" + DOM + """)();
    const project = c => Object.fromEntries(expected.keys.map(k=>[k,c[k]]));
    const controls = state.controls.filter(c=>!['password','hidden','file'].includes(c.type));
    if(state.text!==expected.text || JSON.stringify(controls.map(project))!==JSON.stringify(expected.controls))
       return {clicked:false};
    if(element.disabled || element.getAttribute('aria-disabled')==='true') return {clicked:false};
    element.click();
    return {clicked:true};
}"""


AVAILABILITY_DOM = "(" + DOM + """)({labelFor: element => {
    const explicit = element.getAttribute('aria-label');
    if (explicit) return explicit;
    const label = element.labels?.[0]?.cloneNode(true);
    if (!label) return '';
    label.querySelectorAll('select,input,textarea,button').forEach(control => control.remove());
    return label.textContent.trim();
}})"""


def _availability_control(current, predicate):
    matches = [item for item in current['controls'] if not item['disabled']
               and predicate(item)]
    if len(matches) != 1:
        raise ValueError('The booking page does not expose a supported service and date selector.')
    return matches[0]


def _available_times(current, frame):
    result = []
    for control in current['controls']:
        if (control['frame'] != frame or control['disabled'] or control['tag'] != 'select'
                or not re.fullmatch(r'available (?:appointment )?(?:times|time slots)', control['label'], re.I)):
            continue
        for option in control['options']:
            label = ' '.join(option['label'].split())
            if (option['value'] and not option['disabled']
                    and schema.availability_time(label)
                    and label not in result):
                result.append(label)
    return result[:30]


SERVICE_SELECTOR = re.compile(r'(?:select |choose )?(?:appointment )?service(?: type)?', re.I)
BOOKING_LINK = re.compile(r'(?:book online|book now|book (?:an )?appointment|schedule (?:an )?appointment|appointments)', re.I)
CONTACT_LINK = re.compile(r'contact(?: us)?', re.I)
NON_BOOKING_PATH = re.compile(r'/(?:viewphoto|photo|gallery|menu|contact)(?:[/.~-]|$)', re.I)


def _phone(snapshot):
    # Only return an unambiguous visible North American number or matching tel links.
    links = [item['href'][4:] for item in snapshot['controls'] if item['href'].startswith('tel:')]
    numbers = links or re.findall(r'(?:\+1[-. ]*)?(?:\(\d{3}\)|\b\d{3})[-. ]*\d{3}[-. ]+\d{4}\b', snapshot['text'])
    digits = {re.sub(r'\D', '', number) for number in numbers}
    digits = {number[1:] if len(number) == 11 and number.startswith('1') else number
              for number in digits}
    if len(digits) != 1:
        return ''
    number = digits.pop()
    return f'({number[:3]}) {number[3:6]}-{number[6:]}' if len(number) == 10 else ''


def _booking_links(snapshot):
    return {item['href'] for item in snapshot['controls']
            if BOOKING_LINK.fullmatch(' '.join(item['label'].split()))
            and schema.public_booking_link(item['href'])
            and not NON_BOOKING_PATH.search(unquote(urlsplit(item['href']).path))}


def discover_availability(driver, current, wanted, same_site, observe, check_authority):
    """Verify the business and report only links and contact visible on its site."""
    if not schema.business_matches(current, wanted):
        raise ValueError('I could not verify that business and city on the website.')
    phone = _phone(current)
    links = _booking_links(current)
    if (any(c['tag'] == 'select' and SERVICE_SELECTOR.fullmatch(c['label']) for c in current['controls'])
            and any(c['tag'] == 'input' and c['type'] == 'date' for c in current['controls'])):
        links = {current['url']}
    evidence_url = current['url']
    if not links:
        contacts = {c['href'] for c in current['controls'] if CONTACT_LINK.fullmatch(c['label'])
                    and schema.public_booking_link(c['href']) and same_site(c['href'])
                    and c['href'] != current['url']}
        if len(contacts) == 1:
            check_authority()
            driver.navigate(contacts.pop())
            contact = observe()
            if schema.business_matches(contact, wanted):
                evidence_url = contact['url']
                phone = _phone(contact) or phone
                links = _booking_links(contact)
    check_authority()
    return {'status': 'no_changes', 'mode': 'booking_browser',
            'booking_discovery': {'url': evidence_url, 'phone': phone,
                                  'booking_url': next(iter(links)) if len(links) == 1 else ''}}


def run_availability(driver, proto, spec, allowed):
    """Select native filters in the public context and return observed slots."""
    url = spec['url']
    source = urlsplit(url)

    def same_site(target):
        try:
            parsed = urlsplit(target)
            return (parsed.scheme == 'https' and (parsed.hostname or '').removeprefix('www.') == (source.hostname or '').removeprefix('www.')
                    and parsed.port in (None, 443) and allowed(target))
        except ValueError:
            return False

    def check_authority():
        if not proto.operation('browser.booking', spec['id'], 'Read appointment availability', ''):
            raise ValueError('The appointment availability check is no longer authorized.')

    def guard(route):
        req = route.request
        parsed = urlsplit(req.url)
        target = unquote(parsed.path + '?' + parsed.query)
        write_path = re.search(
            r'/(?:submit|confirm|cancel|delete|checkout|pay)(?:[-/?]|$)'
            r'|[?&](?:action|intent)=(?:confirm|submit|book|reserve|cancel|pay)\b', target, re.I)
        if req.method.upper() not in {'GET', 'HEAD', 'OPTIONS'} or not same_site(req.url) or write_path:
            route.abort()
        else:
            route.fallback()

    def observe():
        current = snapshot(driver, same_site, dom_for_frame=lambda _: AVAILABILITY_DOM)
        if current['blocked']:
            raise ValueError('The booking page requires private sign-in or a human verification step.')
        return current

    check_authority()
    context = driver._context
    context.route_web_socket('**/*', lambda socket: socket.close())
    # Keep the guard installed until the isolated context closes, including
    # requests a page queues after the result is returned.
    context.route('**/*', guard)
    driver.navigate(url)
    current = observe()
    if 'venue' in spec['details']:
        return discover_availability(driver, current, spec['details'], same_site, observe, check_authority)

    def service_control(current):
        return _availability_control(current, lambda item: item['tag'] == 'select'
            and SERVICE_SELECTOR.fullmatch(item['label']))

    def date_control(current):
        return _availability_control(current, lambda item: item['tag'] == 'input' and item['type'] == 'date')

    service = service_control(current)
    date = date_control(current)
    if service['frame'] != date['frame']:
        raise ValueError('Service and date must belong to the same booking form.')
    wanted = spec['details']
    options = [o for o in service['options'] if o['value'] and not o['disabled']
               and ' '.join(o['label'].split()).casefold() == ' '.join(wanted['service'].split()).casefold()]
    if len(options) != 1:
        raise ValueError('The booking page does not show an exact match for that service. Use the service name listed on the booking page.')
    value = options[0]['value']
    original = _available_times(current, service['frame'])
    service_input = locate(driver, service, same_site)
    date_input = locate(driver, date, same_site)
    changed = service_input.input_value() != value or date_input.input_value() != wanted['date']
    check_authority()
    if service_input.input_value() != value:
        service_input.select_option(value, timeout=5000)
    after_service = observe()
    date = date_control(after_service)
    date_input = locate(driver, date, same_site)
    check_authority()
    if date_input.input_value() != wanted['date']:
        original = _available_times(after_service, date['frame']) or original
        date_input.fill(wanted['date'], timeout=5000)
    # Never mistake a previous selection's still-rendered options for results.
    deadline = time.monotonic() + 5
    while True:
        latest = observe()
        service = service_control(latest)
        date = date_control(latest)
        if (service['frame'] == date['frame']
                and locate(driver, service, same_site).input_value() == value
                and locate(driver, date, same_site).input_value() == wanted['date']):
            times = _available_times(latest, service['frame'])
            if times and (not changed or times != original):
                check_authority()
                return {'status': 'no_changes', 'mode': 'booking_browser',
                        'availability': {'service': wanted['service'], 'date': wanted['date'], 'times': times}}
        if time.monotonic() >= deadline:
            break
        driver._page.wait_for_timeout(250)
    check_authority()
    return {'status': 'no_changes', 'mode': 'booking_browser',
            'availability': {'service': wanted['service'], 'date': wanted['date'], 'times': []}}


def run(driver, proto, spec, *, allowlist, root):
    from browser_operator import domain_allowed
    spec=schema.task(spec)
    allowed=lambda u: domain_allowed(u,allowlist)[0]
    if not allowed(spec['url']): raise ValueError('The booking website is outside the permitted sites.')
    if spec['stage'] == schema.AVAILABILITY:
        return run_availability(driver, proto, spec, allowed)
    if not proto.operation('browser.booking',spec['id'],'Prepare or submit the owner-selected booking',''):
        raise ValueError('Booking workflow is not authorized.')
    # Form change handlers cannot silently create a booking before yes.
    # Read-only availability POSTs are bounded to recognized query endpoints
    # and never contain contact information or a GraphQL mutation.
    approved=[False];write_sent=[False];blocked_writes=[];destination=[None]
    def exact_submission(request):
        target=destination[0]
        if not target: return False
        parsed=urlsplit(request.url);expected=urlsplit(target['form_action'])
        return (request.method.upper()==(target.get('form_method') or 'get').upper()
            and (parsed.scheme,parsed.netloc,parsed.path)==(expected.scheme,expected.netloc,expected.path)
            and (request.method.upper()=='GET' or parsed.query==expected.query))
    def availability(request):
        raw=request.post_data or ''
        if re.search(r'mutation|password|credit.?card|payment|phone|email|first.?name|last.?name|guest.?name|(?:^|[&"\s])name["\s]*[=:]',raw,re.I): return False
        if any(str(spec['details'].get(k,'')) and str(spec['details'][k]).casefold() in unquote(raw).casefold() for k in ('email','phone','name')): return False
        path=urlsplit(request.url).path
        if re.search(r'/(?:availability|available-times|slots|search|calendar)(?:/|$)',path,re.I): return True
        try:
            body=json.loads(raw)
            return bool(isinstance(body,dict) and re.match(r'^\s*query\b',str(body.get('query','')),re.I)
                and re.search(r'availability|available.?times|slots',str(body.get('operationName','')),re.I))
        except Exception: return False
    def guard(route):
        req=route.request
        if req.is_navigation_request() and not allowed(req.url): route.abort();return
        raw=unquote((req.url+' '+(req.post_data or ''))).casefold()
        changing=req.method.upper() not in {'GET','HEAD','OPTIONS'} or bool(re.search(r'/(?:submit|confirm|confirm-reservation|cancel-reservation|delete|checkout|pay)(?:[/?]|$)|[?&](?:action|intent)=(?:confirm|submit|book|reserve|cancel)',req.url,re.I))
        private_get=req.method.upper()=='GET' and (bool(re.search(r'[?&](?:phone|email|name|guest_name|first_name|last_name)=',req.url,re.I))
            or any(str(spec['details'].get(k,'')) and str(spec['details'][k]).casefold() in unquote(req.url).casefold() for k in ('email','phone','name')))
        if changing and availability(req): route.fallback();return
        if changing or private_get:
            if not approved[0] or write_sent[0] or not exact_submission(req) or not allowed(req.url) or re.search(r'credit.?card|payment|card.?number|charge.?amount|checkout|billing',raw,re.I):
                blocked_writes.append(req.url);route.abort();return
            write_sent[0]=True
        route.fallback()
    driver._context.route('**/*',guard)
    try:
        driver.navigate(spec['url'])
        for item in spec['steps']:
            if not proto.operation('browser.booking',spec['id'],'Prepare the owner-selected booking',''): raise ValueError('Booking preparation cancelled or unavailable.')
            step(driver,item,spec['details'],allowed)
        current=snapshot(driver,allowed)
        if blocked_writes:
            return {'status':'error','mode':'booking_browser','outcome':'changed' if spec['stage']=='submit' else 'blocked','final_response':'This website attempted an unsupported update while preparing. No booking submission was allowed.'}
        if spec['stage']=='prepare':
            return {'status':'no_changes','mode':'booking_browser','snapshot':current,'final_response':'Booking form inspected; nothing booked.'}
        if schema.review_hash(current)!=spec['expected']:
            return {'status':'error','mode':'booking_browser','outcome':'changed','final_response':'The booking page or availability changed before submission. Nothing was booked; review fresh options.'}
        target=next((c for c in current['controls'] if c['fingerprint']==spec['submit']['fingerprint']),None)
        if not target or target['disabled'] or not schema.COMMIT_LABEL.search(target['label']): raise ValueError('The booking submit control is unavailable.')
        if current.get('blocked') or re.search(r'credit card|card number|deposit|cancellation fee|non-refundable|payment required',current['text'],re.I): raise ValueError('The page requires account access, payment or a fee. Nothing was submitted.')
        # One attempt is claimed before the click and survives host/process loss.
        if not proto.operation('browser.booking',spec['id'],'Submit the exact reviewed booking once',''):
            return {'status':'error','mode':'booking_browser','outcome':'cancelled','final_response':'Submission was stopped before the booking click. Nothing was booked.'}
        if not claim(driver.session_dir,spec['id']):
            return {'status':'error','mode':'booking_browser','outcome':'unknown','final_response':'This booking already had a submission attempt. Check with the business; no retry was made.'}
        try:
            final=snapshot(driver,allowed)
            if schema.review_hash(final)!=spec['expected']:
                return {'status':'error','mode':'booking_browser','outcome':'changed','final_response':'The booking changed before the click. Nothing was booked.'}
            keys=['selector','label','tag','type','value','href','required','disabled','form_action','form_method','options']
            same_frame=[{k:c.get(k) for k in keys} for c in final['controls'] if c['frame']==target['frame']]
            destination[0]=target
            approved[0]=True
            clicked=locate(driver,target,allowed).evaluate(ATOMIC_SUBMIT, {'keys':keys,'controls':same_frame,'text':final['frame_texts'][target['frame']]}, timeout=15000)
            if not clicked or not clicked.get('clicked'):
                return {'status':'error','mode':'booking_browser','outcome':'changed','final_response':'The booking changed before the click. Nothing was booked.'}
            for _ in range(12):
                driver._page.wait_for_timeout(500)
                result=receipt(snapshot(driver,allowed),spec['details'])
                if result['outcome']!='unknown': break
            return {'status':'changes','mode':'booking_browser',**result,'final_response':'Booking submission checked against the actual page.'}
        except Exception:
            return {'status':'error','mode':'booking_browser','outcome':'unknown','final_response':'The booking outcome is unknown after starting submission. Check the business before trying again. No automatic retry.'}
    finally:
        try: driver._context.unroute('**/*',guard)
        except Exception: pass
