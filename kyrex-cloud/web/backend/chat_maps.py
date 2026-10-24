"""Turn-scoped device location and Google Maps read adapter; no saved GPS trail."""
import math
import os
import re
import time
from urllib.parse import urlencode

import requests

MODES = {'driving': 'DRIVE', 'walking': 'WALK', 'bicycling': 'BICYCLE', 'transit': 'TRANSIT'}
REQUEST = re.compile(r'\b(?:how far(?!\s+(?:along|into|back|ahead)\b)|how long.*(?:drive|walk|travel|get to)|(?:distance|travel time) (?:to|from|between)|directions? to|navigate to|route to|take me to|near me|nearby|closest|nearest|current location)\b|^\s*where am i[?.!]*\s*$', re.I)


def requested(text):
    return bool(REQUEST.search(str(text or '')))


def validate_location(value):
    if value is None:
        return {'status': 'not_shared'}
    if not isinstance(value, dict):
        raise ValueError('Invalid device location.')
    status = value.get('status')
    if status in {'not_shared', 'denied', 'unavailable'}:
        return {'status': status}
    if status != 'available':
        raise ValueError('Invalid device location.')
    fields = {'latitude': (-90, 90), 'longitude': (-180, 180),
              'accuracy_meters': (0, 100000), 'captured_at': (0, float('inf'))}
    out = {'status': status}
    for key, (low, high) in fields.items():
        number = value.get(key)
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) or not low <= number <= high):
            raise ValueError('Invalid device location.')
        out[key] = number
    if not -30000 <= time.time() * 1000 - out['captured_at'] <= 300000:
        return {'status': 'unavailable'}
    return out


def bot_allowed(owner, bot_id=None):
    """The owner's route request grants this public read; live explicit denies win."""
    if not bot_id:
        return True
    import bots
    import bot_capabilities
    import policy
    try:
        bot = bots.get_bot(bot_id)
        if bot.get('owner') != owner or not bots.is_running(bot):
            return False
        rules = dict(bot.get('policy') or {})
        bot_capabilities.validate_policy(rules)
        decision = policy.evaluate(rules, 'maps:read', 0)
        return decision['matched_rule'] is None or decision['effective_tier'] == 0
    except Exception:
        return False


def context(location, tool_available=True):
    status = (location or {}).get('status', 'not_shared')
    text = ('\n\nFor this turn, device location is ' + status + '. '
            'Use maps_route for location, driving distance, travel time or directions questions. '
            'The host holds any device coordinates; they are not a saved home address. '
            'Default to the device origin for "from me" or "here". Never substitute a remembered '
            'town or town-to-town estimate. If location is not shared, offer the composer’s '
            'Use current location control or ask for a starting address. Provide the returned '
            'Open in Google Maps link. Google Maps can use its own current location when opened. '
            'Only report miles and minutes from a verified route result. If the Routes API '
            'is not configured or fails, say the route estimate is unavailable here and link '
            'to Google Maps; do not look up a generic town distance as a substitute. '
            'Resolve an ambiguous destination before claiming a store-specific distance.')
    if not tool_available:
        text += (' The maps_route tool is unavailable in this chat mode or Bot policy. '
                 'Offer a Google Maps link https://www.google.com/maps/dir/?api=1&destination= '
                 'with the destination URL-encoded and travelmode=driving. Omit the origin '
                 'so Maps can use its own current location. No in-chat miles/minutes are verified.')
    return text


def _place(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 500 or any(ord(c) < 32 for c in value):
        raise ValueError('Provide a destination or starting address, up to 500 characters.')
    return value.strip()


def maps_url(destination, origin='', mode='driving', place_id=None):
    query = {'api': '1', 'destination': destination, 'travelmode': mode, 'dir_action': 'navigate'}
    if origin:
        query['origin'] = origin
    if place_id:
        query['destination_place_id'] = place_id
    return 'https://www.google.com/maps/dir/?' + urlencode(query)


def route(destination, location, *, origin='', mode='driving', authorize=lambda: True):
    destination = _place(destination)
    if mode not in MODES:
        raise ValueError('Choose driving, walking, bicycling or transit.')
    origin = _place(origin) if origin else ''
    location = validate_location(location)
    link = maps_url(destination, origin, mode)
    result = {'status': 'maps_only', 'destination': destination, 'travel_mode': mode,
              'maps_url': link, 'source': 'Google Maps',
              'instruction': 'Include the Open in Google Maps link. No verified distance or travel time is available; do not estimate one.'}
    if not authorize():
        raise ValueError('Maps access was stopped or changed.')
    if not origin and location['status'] != 'available':
        return {**result, 'status': 'needs_origin', 'reason': 'Share current location or provide a starting address. Google Maps can use its device location when opened.'}
    if not origin and location['accuracy_meters'] > 1000:
        return {**result, 'status': 'needs_origin', 'reason': 'Device location is too approximate for a useful route. Try again or provide a starting address.'}
    key = os.environ.get('GOOGLE_MAPS_API_KEY', '').strip()
    if not key:
        return {**result, 'reason': 'Google Routes API is not configured. Open Google Maps for the route and travel time.'}
    start = {'address': origin} if origin else {'location': {'latLng': {
        'latitude': location['latitude'], 'longitude': location['longitude']}}}
    payload = {'origin': start, 'destination': {'address': destination}, 'travelMode': MODES[mode],
               'units': 'IMPERIAL'}
    if mode == 'driving':
        payload['routingPreference'] = 'TRAFFIC_AWARE'
    try:
        if not authorize():
            raise ValueError('Maps access was stopped or changed.')
        response = requests.post('https://routes.googleapis.com/directions/v2:computeRoutes',
            headers={'X-Goog-Api-Key': key, 'X-Goog-FieldMask': 'routes.distanceMeters,routes.duration,geocodingResults'},
            json=payload, timeout=(3, 8), allow_redirects=False)
        if not authorize():
            raise ValueError('Maps access was stopped or changed.')
        if response.status_code != 200 or len(response.content) > 100000:
            return {**result, 'status': 'route_unavailable', 'reason': 'Google could not provide a route estimate. Open Google Maps to check it.'}
        data = response.json()
        # Reject partial/address-area geocodes rather than measuring to a town center.
        geocodes = data.get('geocodingResults') or {}
        for waypoint in ('origin', 'destination'):
            geo = geocodes.get(waypoint) or {}
            types = set(geo.get('type') or [])
            if geo.get('partialMatch') or (types and types <= {'locality', 'political', 'country', 'postal_code', 'administrative_area_level_1', 'administrative_area_level_2', 'route'}):
                return {**result, 'status': 'needs_destination', 'reason': 'Google only matched an area or partial address. Ask for the exact starting address or destination.'}
        found = (data.get('routes') or [])[0]
        distance = found['distanceMeters']
        seconds = float(re.fullmatch(r'(\d+(?:\.\d+)?)s', found['duration']).group(1))
        if isinstance(distance, bool) or not isinstance(distance, (int, float)) or not math.isfinite(distance) or distance < 0 or not math.isfinite(seconds):
            raise ValueError('Invalid route result.')
        place_id = (geocodes.get('destination') or {}).get('placeId')
        return {**result, 'status': 'verified', 'distance_meters': distance, 'duration_seconds': seconds,
                'distance_miles': round(distance / 1609.344, 1), 'duration_minutes': max(1, round(seconds / 60)),
                'origin': origin or 'Current device location', 'location_accuracy_meters': None if origin else location['accuracy_meters'],
                'maps_url': maps_url(destination, origin, mode, place_id), 'source': 'Google Routes API',
                'instruction': 'Report this estimated route distance and travel time, say it starts from the device fix or stated address, and include Open in Google Maps. Travel time can change.'}
    except (requests.RequestException, ValueError, TypeError, KeyError, IndexError, AttributeError):
        return {**result, 'status': 'route_unavailable', 'reason': 'The route estimate is unavailable. Open Google Maps to check it.'}
