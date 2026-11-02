"""Exact reservation cancellation under the existing workout review authority."""
import re
from datetime import datetime

import booking_schema as common
import member_bookings_schema as member
import workout_booking_schema as workout


def browser_problem(exc):
    """Fixed stage/structural evidence; never expose exception or portal text."""
    detail = workout.browser_failure(exc)
    if isinstance(exc, member.ReadFailure):
        message = member.failure_message(member.failure(exc))
    else:
        message = 'The portal cancellation could not be verified.'
    return (message + ' Failed while ' + workout.FAILURE_PHASES[detail['phase']] + '.'
        + ' ' + detail['error_type'] + ' at ' + detail['location'] + '.'
        + ' No cancellation was submitted.')


def request(url, method, body, identity, reference):
    if (not isinstance(reference, str) or not re.fullmatch(r'[a-f0-9]{24}', reference)
            or url != 'https://api.glofox.com/2.0/bookings/' + reference
            or method != 'DELETE' or body not in (None, '')
            or not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{64}', identity)):
        raise ValueError('The portal cancellation request does not match the reviewed reservation.')
    return {'url': url, 'method': method, 'identity': identity}


def terms(text):
    text = ' '.join(str(text or '').split())
    # Copy shown in the owner's Glofox mobile confirmation screenshot. This
    # recognizes wording only: the Host still requires the existing portal
    # frame, dialog/control, member binding and intercepted native DELETE.
    if text in (
        'Cancel booking Are you sure you want to cancel your booking? CANCEL BOOKING NO',
        'Cancel booking Are you sure you want to cancel your booking? Cancel booking No',
    ):
        return text
    # These are the portal's own no-penalty confirmation messages. Its paid,
    # late-cancel and disabled variants have different copy and stay blocked.
    allowed = (
        'Are you sure you want to cancel your booking?',
        'Are you sure you want to cancel this booking? No fees will be charged if you cancel now.',
    )
    for message in allowed:
        if text == 'Confirm booking cancellation ' + message + ' Cancel booking':
            return text
    if re.fullmatch(r"Confirm booking cancellation You can cancel this booking now without a fee\. "
        r"If you don[’']t cancel and don[’']t attend, a \$[0-9]{1,4}(?:\.[0-9]{1,2})? no-show fee applies\. "
        r"Free classes are exempt\. Cancel booking", text):
        return text
    raise ValueError('The portal did not show a supported cancellation without fees. Review it in Glofox.')


def digest(spec, control, text, native):
    common.control(control)
    if (common.fingerprint(control) != control['fingerprint']
            or not workout.portal_url(control['frame']) or control.get('disabled')
            or control.get('selector') != 'css=#general-dialog-confirm-button'
            or control.get('label', '').casefold() != 'cancel booking'):
        raise ValueError('The cancellation confirmation control changed.')
    if not isinstance(native, dict) or native != request(native.get('url'), native.get('method'), None,
                         spec['identity'], spec['details']['reference']):
        raise ValueError('The cancellation request changed.')
    return common.digest({'operation': 'cancel', 'details': spec['details'],
        'window': spec['window'], 'identity': spec['identity'], 'terms': terms(text),
        'control': {k: v for k, v in control.items() if k != 'ref'}, 'request': native})


def task(value):
    required = {'id', 'nonce', 'stage', 'url', 'details', 'steps', 'operation', 'window', 'identity'}
    if (not isinstance(value, dict) or value.get('operation') != 'cancel'
            or value.get('stage') not in {'prepare', 'submit'}
            or set(value) != required | ({'expected', 'submit', 'request', 'terms'} if value.get('stage') == 'submit' else set())
            or not re.fullmatch(r'[a-f0-9]{32}', str(value.get('id', '')))
            or not re.fullmatch(r'[a-f0-9]{64}', str(value.get('nonce', '')))
            or value.get('url') != workout.URL or value.get('steps') != []):
        raise ValueError('Invalid reservation cancellation task.')
    member.result([value['details']], value['identity'], value['window'])
    if value['details']['title'] != 'Group Fitness Class':
        raise ValueError('Only the supported booked class can be cancelled.')
    if value['stage'] == 'submit':
        if not re.fullmatch(r'[a-f0-9]{64}', str(value['expected'])):
            raise ValueError('Cancellation requires an exact owner review.')
        if value['expected'] != digest(value, value['submit'], value['terms'], value['request']):
            raise ValueError('The cancellation review changed.')
    return value


def label(details):
    start = datetime.fromtimestamp(details['start'], member.TZ)
    return f"Level 6 · {details['title']} · {start:%A, %B %d, %Y at %I:%M %p} ET"


def confirmed(data):
    return f"Glofox confirmed cancellation of **{label(data['details'])}**. Reservation reference: **{data['details']['reference']}**."

