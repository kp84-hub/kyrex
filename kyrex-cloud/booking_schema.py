"""Shared bounded booking wire schema. Data never supplies approval authority."""
import hashlib
import json
import re
from datetime import date, datetime
from urllib.parse import urlsplit

ACTION = 'booking_browser'
FIELDS = ('venue', 'location', 'date', 'time', 'party_size', 'name', 'phone', 'email')
FORM_FIELDS = FIELDS + ('first_name','last_name')
PREPARE_LABEL = re.compile(r'^(?:search|find(?: a)?(?: table| time)?|check availability|show (?:times|availability)|availability|select (?:date|time|party size)|choose (?:date|time)|next month|previous month|[0-9]{1,2}|[0-9]{1,2}:[0-9]{2}(?:\s*[ap]m)?)$', re.I)
COMMIT_LABEL = re.compile(r'\b(?:book|reserve|reservation|confirm|complete|submit|request appointment)\b', re.I)
FORBIDDEN = re.compile(r'\b(?:pay|payment|purchase|checkout|credit card|card number|cancellation fee|non-refundable|deposit|delete|password|sign in|log in|captcha|verify you are human)\b', re.I)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def clean(value, maximum=200):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid booking detail.')
    return value.strip()


def details(values):
    if not isinstance(values, dict) or set(values) - set(FIELDS):
        raise ValueError('Invalid booking fields.')
    result = {}
    for key, value in values.items():
        if value in ('', None): continue
        if key == 'party_size':
            if type(value) is not int or not 1 <= value <= 30: raise ValueError('Use a party size from 1 to 30.')
            result[key] = value
        else:
            result[key] = clean(value)
    if 'date' in result and date.fromisoformat(result['date']).isoformat() != result['date']: raise ValueError('Use a complete booking date.')
    if 'time' in result and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', result['time']): raise ValueError('Use a complete booking time.')
    if 'email' in result and not re.fullmatch(r'[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', result['email']): raise ValueError('Use a complete contact email.')
    if 'phone' in result and (not re.fullmatch(r'[+0-9 () .-]{10,30}', result['phone']) or not 10 <= len(re.sub(r'\D','',result['phone'])) <= 15): raise ValueError('Use a complete contact phone number.')
    return result


def matches(field, actual, wanted):
    actual, wanted = str(actual).strip(), str(wanted).strip()
    if field == 'phone': return re.sub(r'\D', '', actual) == re.sub(r'\D', '', wanted)
    if field == 'party_size': return actual == wanted or bool(re.fullmatch(rf'{re.escape(wanted)}\s*(?:people|persons|guests|person)', actual, re.I))
    if field == 'time':
        for fmt in ('%H:%M', '%I:%M %p', '%I:%M%p'):
            try:
                if datetime.strptime(actual.upper(), fmt).strftime('%H:%M') == wanted: return True
            except ValueError: pass
        return False
    if field == 'date':
        for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%B %d, %Y', '%b %d, %Y', '%A, %B %d, %Y'):
            try:
                if datetime.strptime(actual, fmt).date().isoformat() == wanted: return True
            except ValueError: pass
        return False
    return actual.casefold() == wanted.casefold()


def task(value):
    if (not isinstance(value, dict) or set(value) - {'id','nonce','url','steps','stage','details','expected','submit'}
            or not re.fullmatch(r'[a-f0-9]{32}', str(value.get('id','')))
            or not re.fullmatch(r'[a-f0-9]{64}', str(value.get('nonce','')))
            or value.get('stage') not in {'prepare','submit'}):
        raise ValueError('Invalid booking browser task.')
    parsed = urlsplit(str(value.get('url','')))
    if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or len(value['url']) > 2048:
        raise ValueError('Invalid booking page URL.')
    details(value.get('details', {}))
    steps = value.get('steps')
    if not isinstance(steps, list) or len(steps) > 20: raise ValueError('Booking preparation exceeded its step limit.')
    for step in steps:
        if (not isinstance(step, dict) or set(step) - {'op','control','field','value'}
                or step.get('op') not in {'fill','select','click','link'}): raise ValueError('Invalid booking preparation step.')
        control(step.get('control'))
        if step['op'] in {'fill','select'}:
            if step.get('field') not in FORM_FIELDS: raise ValueError('Invalid booking field.')
            clean(step.get('value'), 200)
            if not matches(step['field'], step['value'], field_value(value['details'], step['field'])):
                raise ValueError('The form value does not match the owner booking detail.')
        elif step.get('value') or step.get('field'): raise ValueError('Clicks cannot carry booking field values.')
    if value['stage'] == 'submit':
        control(value.get('submit'))
        if not re.fullmatch(r'[a-f0-9]{64}', str(value.get('expected',''))): raise ValueError('A booking needs an exact review snapshot.')
        if FORBIDDEN.search(value['submit'].get('label','')): raise ValueError('Unsupported booking submission.')
    elif 'submit' in value or 'expected' in value: raise ValueError('Preparation cannot approve submission.')
    return value


def control(value):
    if (not isinstance(value, dict) or set(value) - {'ref','selector','frame','fingerprint','label','tag','type','value','href','options','required','disabled','form_action','form_method'}
            or not str(value.get('selector','')).startswith('css=')
            or len(value.get('selector','')) > 1024
            or not re.fullmatch(r'[a-f0-9]{64}', str(value.get('fingerprint','')))
            or not isinstance(value.get('frame'), str)):
        raise ValueError('Invalid observed booking control.')
    return value


def fingerprint(control):
    return digest({key: control.get(key,'') for key in ('selector','frame','label','tag','type','href','form_action','form_method')})


def review_hash(snapshot):
    # Values and visible terms are bound; cookies, tokens and hidden CSRF fields never leave the page.
    return digest({'url': snapshot['url'], 'text': snapshot['text'],
                   'controls': snapshot['controls']})


def field_value(details, field):
    if field in {'first_name','last_name'}:
        parts=str(details.get('name','')).split(' ',1)
        return parts[0] if field=='first_name' else parts[1] if len(parts)>1 else ''
    return details.get(field,'')


def field_allowed(field, item):
    label=item.get('label','')
    if re.search(r'password|token|secret|birth|expir|credit|card|payment',label,re.I): return False
    aliases={'name':r'full.?name|your name|^name\b|reservation.?name|booking.?name', 'first_name':r'first.?name|given.?name|fname',
        'last_name':r'last.?name|surname|family.?name|lname', 'phone':r'phone|mobile|telephone|tel',
        'email':r'e.?mail', 'date':r'date|day', 'time':r'time|seating',
        'party_size':r'party|people|guests|persons|covers', 'venue':r'restaurant|venue|search', 'location':r'location|city|area|postal|zip'}
    return bool(re.search(aliases.get(field,r'(?!)'),label,re.I)
        or field=='email' and item.get('type')=='email' or field=='phone' and item.get('type')=='tel'
        or field=='date' and item.get('type')=='date' or field=='time' and item.get('type')=='time')


def business_matches(snapshot, details):
    text=re.sub(r'[^a-z0-9]','',str(snapshot.get('title','')+' '+snapshot.get('text','')).casefold())
    return all(re.sub(r'[^a-z0-9]','',str(details.get(k,'')).casefold()) in text for k in ('venue','location') if details.get(k))
