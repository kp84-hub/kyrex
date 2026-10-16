"""Read-only Glofox member-booking adapter contract, separate from submission."""
import hashlib
import json
import re
from datetime import date, datetime, time, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from workout_booking_schema import BRANCH, HOSTS, URL

ACTION = 'member_bookings'
STUDIO_LABEL = 'Level 6'  # existing supported adapter, selected by an owner request
TZ = ZoneInfo('America/New_York')  # the existing supported studio adapter
MAX_PAGES = 4
PAGE_SIZE = 100
MAX_BYTES = 512 * 1024

# Session maintenance observed in the portal's own AuthenticationService.
# Keep these separate from the working booking adapter's host/wire contract.
REFRESH_URL = 'https://auth.glofox.com/refresh'
READER_HOSTS = (*HOSTS, 'auth.glofox.com')
FAILURES = {
    'signed_out': 'The member portal shows you are signed out. Sign in through the Browser Bot private viewer, then close the viewer.',
    'session_unverified': 'The member portal loaded, but its saved session could not be verified. Check it in the Browser Bot private viewer; this is not proof that you are signed out.',
    'unsupported_portal': 'The supported Glofox member portal did not load. Check Browser Host site access and reader compatibility.',
    'refresh_blocked': 'The reader could not allow the portal session refresh. Check Browser Host site permission for auth.glofox.com.',
    'missing_read': 'The signed-in portal did not expose a supported My bookings read. Check reader compatibility.',
    'unavailable': 'The member bookings could not be verified. Check the Browser Host session and adapter.',
}


class ReadFailure(ValueError):
    def __init__(self, code):
        self.code = code if code in FAILURES else 'unavailable'
        super().__init__(FAILURES[self.code])


def failure(exc):
    # Never transmit exception text, URLs, response bodies, or token values.
    return {'code': exc.code if isinstance(exc, ReadFailure) else 'unavailable'}


def failure_message(detail):
    code = detail.get('code') if isinstance(detail, dict) else None
    return FAILURES.get(code if isinstance(code, str) else None, FAILURES['unavailable'])


def parse(value):
    if not isinstance(value, dict) or set(value) != {'url', 'start', 'end'} or value['url'] != URL:
        raise ValueError('Invalid member-bookings read.')
    try:
        first, last = (date.fromisoformat(value[k]) for k in ('start', 'end'))
    except (ValueError, TypeError):
        raise ValueError('Invalid booking read dates.') from None
    if first.isoformat() != value['start'] or last.isoformat() != value['end'] or not 0 < (last - first).days <= 31:
        raise ValueError('Choose a booking window of at most 31 days.')
    return dict(value)


def bounds(spec):
    spec = parse(spec)
    return tuple(int(datetime.combine(date.fromisoformat(spec[k]), time.min, TZ).timestamp())
                 for k in ('start', 'end'))


def native_member(url):
    """Accept only the portal's own documented, member-specific GET target.

    Identity comes from the signed-in portal's request, never user/model text.
    No studio-wide booking endpoint, family selection or caller-supplied ID.
    """
    p = urlsplit(url)
    paths = {'api.glofox.com': '/2.0/bookings',
             'gf-api.aws.glofox.com': '/prod/2.0/bookings'}
    if (p.scheme != 'https' or p.netloc not in paths or p.path != paths[p.netloc]
            or p.fragment or p.username or p.password):
        return None
    q = parse_qs(p.query, keep_blank_values=True)
    user = q.get('user_id', [])
    if len(user) != 1 or not re.fullmatch(r'[a-f0-9]{24}', user[0]):
        return None
    for key in ('branchId', 'branch_id'):
        if key in q and q[key] != [BRANCH]:
            return None
    return user[0]


def read_url(native, spec, page):
    member = native_member(native)
    if not member or type(page) is not int or not 1 <= page <= MAX_PAGES:
        raise ValueError('The portal did not identify one member-bookings read.')
    start, end = bounds(spec)
    p = urlsplit(native)
    # Discard every native filter; include all statuses, never just one class.
    query = {'branchId': BRANCH, 'user_id': member, 'sort_by': 'time_start',
             'limit': PAGE_SIZE, 'page': page, 'time_start': start, 'time_end': end}
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ''))


def clean(value, limit=160):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 for c in value)):
        raise ValueError('A booked class has unreadable details.')
    return value.strip()


def page_rows(payload, member, spec, page):
    if (not isinstance(payload, dict) or payload.get('success') is False
            or payload.get('object') != 'list' or type(payload.get('page')) is not int or payload.get('page') != page
            or type(payload.get('has_more')) is not bool
            or type(payload.get('total_count')) is not int
            or not 0 <= payload['total_count'] <= MAX_PAGES * PAGE_SIZE
            or not isinstance(payload.get('data'), list)
            or len(payload['data']) > PAGE_SIZE):
        raise ValueError('The member-bookings response was incomplete or unsupported.')
    low, high = bounds(spec)
    rows = []
    for raw in payload['data']:
        if (not isinstance(raw, dict) or raw.get('branch_id') != BRANCH
                or raw.get('user_id') != member):
            raise ValueError('The booking response belongs to another member or studio.')
        if raw.get('status') not in {'BOOKED', 'WAITING', 'CANCELED', 'CANCELLED', 'RESERVED', 'FAILED'}:
            raise ValueError('A booking has an unsupported status.')
        if raw['status'] != 'BOOKED' or raw.get('confirmed') is False:
            continue
        if raw.get('model', raw.get('type')) not in {'event', 'events'}:
            continue
        start, duration = raw.get('time_start'), raw.get('duration')
        if type(start) is not int or type(duration) is not int or not 1 <= duration <= 480:
            raise ValueError('A booked class has unreadable times.')
        if not low <= start < high:
            continue
        ref, event = raw.get('_id'), raw.get('event_id') or raw.get('model_id')
        if any(not isinstance(v, str) or not re.fullmatch(r'[a-f0-9]{24}', v) for v in (ref, event)):
            raise ValueError('A booked class has no verified reference.')
        title = clean(raw.get('model_name') or raw.get('event_name'))
        rows.append({'reference': ref, 'event_id': event, 'title': title,
                     'start': start, 'end': start + duration * 60})
    return rows


def result(rows, identity, spec):
    """Validate even host-returned data before it becomes calendar evidence."""
    parse(spec)
    low, high = bounds(spec)
    if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{64}', identity):
        raise ValueError('The member session could not be bound to this read.')
    if not isinstance(rows, list) or len(rows) > MAX_PAGES * PAGE_SIZE:
        raise ValueError('The booking list is incomplete.')
    seen, slots = set(), set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'reference', 'event_id', 'title', 'start', 'end'}
                or any(not isinstance(row.get(k), str) or not re.fullmatch(r'[a-f0-9]{24}', row[k])
                       for k in ('reference', 'event_id'))
                or type(row.get('start')) is not int or type(row.get('end')) is not int
                or not low <= row['start'] < high or not 0 < row['end'] - row['start'] <= 480 * 60
                or row['reference'] in seen or (row['event_id'], row['start']) in slots):
            raise ValueError('The booking list is malformed or duplicated.')
        clean(row['title'])
        seen.add(row['reference'])
        slots.add((row['event_id'], row['start']))
    return {'mode': ACTION, 'status': 'no_changes', 'complete': True,
            'window': dict(spec), 'identity': identity,
            'bookings': sorted(rows, key=lambda r: (r['start'], r['reference']))}


def session_identity(secret, member):
    return hashlib.sha256((secret + '\0' + member).encode()).hexdigest()
