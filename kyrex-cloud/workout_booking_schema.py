"""Fixed Level 6 member-portal booking wire; no credentials or arbitrary APIs."""
import json
import re
from datetime import datetime
from urllib.parse import urlsplit

import booking_schema as common

ACTION = 'workout_browser'
BRANCH = '67239a5afaac4eea10045d29'
URL = 'https://level6training.com/book-a-class'
HOSTS = ('level6training.com', 'app.glofox.com', 'api.glofox.com', 'gf-api.aws.glofox.com')


def portal_url(url):
    parsed = urlsplit(url)
    return (parsed.scheme == 'https' and parsed.hostname == 'app.glofox.com'
            and parsed.path == '/portal/' and not parsed.username and not parsed.password
            and re.match(r'^/branch/' + BRANCH + r'(?:/|$)', parsed.fragment))


def request_template(url, method, body, identity):
    """Only the documented member endpoint with explicit no-waitlist intent."""
    parsed = urlsplit(url)
    paths = ('/2.3/branches/' + BRANCH + '/bookings',
             '/prod/2.3/branches/' + BRANCH + '/bookings')
    if (method != 'POST' or parsed.scheme != 'https'
            or parsed.hostname not in HOSTS[2:] or parsed.path not in paths
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError('This portal uses a booking endpoint that needs a verified adapter.')
    allowed = {'model', 'model_id', 'guest_bookings', 'join_waiting_list', 'charge',
               'user_id', 'branch_id', 'payment_method', 'price', 'pay_gym'}
    if (not isinstance(body, dict) or set(body) - allowed or body.get('model') != 'event'
            or not re.fullmatch(r'[a-f0-9]{24}', str(body.get('model_id', '')))
            or body.get('join_waiting_list') is not False
            or type(body.get('guest_bookings', 0)) is not int or body.get('guest_bookings', 0) != 0
            or body.get('branch_id', BRANCH) != BRANCH
            or body.get('price', 0) != 0 or isinstance(body.get('price', 0), bool)
            or body.get('pay_gym', False) is not False
            or body.get('payment_method', 'membership') not in {'membership', 'credits', 'credit', 'free'}
            or 'charge' in body and type(body['charge']) is not bool
            or 'user_id' in body and not re.fullmatch(r'[a-f0-9]{24}', str(body['user_id']))
            or not re.fullmatch(r'[a-f0-9]{64}', str(identity))):
        raise ValueError('The native booking request requires unsupported payment, guests or waitlist access.')
    return {'url': url, 'method': method, 'body': body, 'identity': identity}


def task(value):
    allowed = {'id', 'nonce', 'stage', 'url', 'details', 'steps', 'expected', 'submit', 'request'}
    if (not isinstance(value, dict) or set(value) - allowed
            or not re.fullmatch(r'[a-f0-9]{32}', str(value.get('id', '')))
            or not re.fullmatch(r'[a-f0-9]{64}', str(value.get('nonce', '')))
            or value.get('stage') not in {'prepare', 'submit'} or value.get('url') != URL):
        raise ValueError('Invalid Level 6 browser task.')
    details = value.get('details', {})
    if not isinstance(details, dict) or set(details) != {'date', 'time', 'event_id', 'class_name', 'trainer_name', 'start'}:
        raise ValueError('Invalid Level 6 class details.')
    common.details({k: details[k] for k in ('date', 'time')})
    if (not re.fullmatch(r'[a-f0-9]{24}', str(details['event_id']))
            or details['class_name'] != 'Group Fitness Class' or type(details['start']) is not int):
        raise ValueError('The workout is not a verified Level 6 class.')
    common.clean(details['trainer_name'], 120)
    from zoneinfo import ZoneInfo
    local = datetime.fromtimestamp(details['start'], ZoneInfo('America/New_York'))
    if local.strftime('%Y-%m-%d %H:%M') != details['date'] + ' ' + details['time']:
        raise ValueError('The class timestamp does not match the requested time.')
    steps = value.get('steps')
    if not isinstance(steps, list) or len(steps) > 18:
        raise ValueError('Too many workout preparation steps.')
    for step in steps:
        if not isinstance(step, dict) or set(step) - {'op', 'control', 'field', 'value'}:
            raise ValueError('Invalid workout preparation step.')
        common.control(step.get('control'))
        if not portal_url(step['control']['frame']):
            raise ValueError('Workout controls must belong to the Level 6 portal.')
        if step.get('op') in {'fill', 'select'}:
            if step.get('field') not in {'date', 'time'} or not common.matches(
                    step['field'], step.get('value'), details[step['field']]):
                raise ValueError('Workout form values must match your requested date and time.')
        elif step.get('op') != 'click' or step.get('field') or step.get('value'):
            raise ValueError('Unsupported workout preparation action.')
    if value['stage'] == 'submit':
        common.control(value.get('submit'))
        if not portal_url(value['submit']['frame']):
            raise ValueError('The submit control left the Level 6 portal.')
        if not re.fullmatch(r'[a-f0-9]{64}', str(value.get('expected', ''))):
            raise ValueError('Workout submission needs an exact owner review.')
        request = value.get('request', {})
        request_template(request.get('url', ''), request.get('method'), request.get('body'), request.get('identity'))
        if request['body']['model_id'] != details['event_id']:
            raise ValueError('The booking request targets a different class.')
    elif any(k in value for k in ('submit', 'expected', 'request')):
        raise ValueError('Workout preparation cannot approve submission.')
    return value


def wire(text):
    value = json.loads(text)
    if set(value) == {ACTION}:
        return ACTION, task(value[ACTION])
    if set(value) == {common.ACTION}:
        return common.ACTION, common.task(value[common.ACTION])
    raise ValueError('Invalid fixed booking task.')
