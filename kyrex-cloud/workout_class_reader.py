"""Bounded public lookup for one native workout proposal; no booking writes."""
from datetime import datetime, time, timedelta

import glofox_api as api


def class_at(day: str, clock: str) -> dict:
    """One exact public class lookup; member booking remains a separate workflow."""
    import re
    from booking_schema import details as validate
    validate({'date': day, 'time': clock})
    start = datetime.fromisoformat(day + 'T' + clock).replace(tzinfo=api.BRANCH_TZ)
    now = datetime.now(api.BRANCH_TZ)
    if not now < start <= now + timedelta(days=14):
        raise api.GlofoxDataError('Choose a future Level 6 class within the next 14 days.')
    token = api._post_guest_login()
    try:
        api.get_branch(token)
        events = api._query_week_events(token, datetime.combine(start.date(), time.min, api.BRANCH_TZ),
                                   datetime.combine(start.date(), time(23, 59, 59), api.BRANCH_TZ))
        trainers = api.get_trainers(token)
    finally:
        token = ''
    matches = [e for e in events if e['time_start'] == int(start.timestamp())
               and e['name'] == api.SCHEDULED_CLASS_NAME]
    if len(matches) != 1:
        raise api.GlofoxDataError('I could not find one Level 6 Group Fitness Class at that exact time.')
    event = matches[0]
    if (not re.fullmatch(r'[a-f0-9]{24}', event['_id'])
            or event.get('status') != 'AVAILABLE'
            or type(event.get('size')) is not int or type(event.get('booked')) is not int
            or not 0 <= event['booked'] < event['size']):
        raise api.GlofoxDataError('That Level 6 class is full or unavailable. I have not joined a waitlist.')
    names = []
    for trainer in event['trainers']:
        record = trainers.get(trainer)
        if not record:
            raise api.GlofoxDataError('The class trainer could not be verified.')
        names.append(record)
    if not names:
        raise api.GlofoxDataError('The class trainer could not be verified.')
    name = ', '.join(names)
    if len(name) > 120 or any(ord(c) < 32 for c in name):
        raise api.GlofoxDataError('The class trainer could not be displayed safely.')
    return {'date': day, 'time': clock, 'event_id': event['_id'],
            'class_name': api.SCHEDULED_CLASS_NAME, 'trainer_name': name,
            'start': int(start.timestamp())}
