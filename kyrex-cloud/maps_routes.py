"""Server-side, owner-scoped Google Routes driving lookup.

One pinned HTTPS endpoint, one recommended route, bounded transport, no stored
location history. Models receive only the safe route projection, never a key.
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

ENDPOINT = 'https://routes.googleapis.com/directions/v2:computeRoutes'
FIELD_MASK = 'routes.duration,routes.distanceMeters,geocodingResults.origin,geocodingResults.destination,fallbackInfo'
MAX_RESPONSE = 64_000
TIMEOUT = 15


class MapsError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def owner_enabled(owner):
    owners = os.environ.get('KYREX_MAPS_ROUTE_OWNERS', '').split(',')
    return bool(str(owner or '').strip() and str(owner).strip() in {v.strip() for v in owners if v.strip()}
                and os.environ.get('GOOGLE_MAPS_API_KEY', '').strip())


def bot_enabled(bot):
    import bots
    if not isinstance(bot, dict) or not bots.is_running(bot) or not owner_enabled(bot.get('owner')):
        return False
    rules = bot.get('policy')
    if not isinstance(rules, dict):
        return False
    # Explicit Maps restrictions remain authoritative. Legacy *:deny presets
    # do not remove the owner's separately enabled connected-service grant.
    for rule in ('maps:route', 'maps:*'):
        if rule in rules:
            value = rules[rule]
            return type(value) is int and value == 0
    return True


def _address(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 500 or any(ord(c) < 32 for c in value):
        raise MapsError('invalid_location', f'{field} needs a specific place/address (maximum 500 characters).')
    return value.strip()


def _departure(value, now):
    if value in (None, '', 'now'):
        return now, None
    if not isinstance(value, str) or len(value) > 40:
        raise MapsError('invalid_departure', 'Departure must be now or an ISO timestamp with timezone offset.')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        parsed = parsed.astimezone(timezone.utc)
    except ValueError:
        raise MapsError('invalid_departure', 'Departure must include the date, time and timezone offset.') from None
    if parsed < now:
        raise MapsError('past_departure', 'Driving routes require a current or future departure time.')
    if parsed > now + timedelta(days=365):
        raise MapsError('invalid_departure', 'Departure must be within the next year.')
    return parsed, parsed.isoformat().replace('+00:00', 'Z')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(payload, key):
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(), method='POST', headers={
        'Content-Type': 'application/json', 'X-Goog-Api-Key': key, 'X-Goog-FieldMask': FIELD_MASK})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=TIMEOUT) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        code = 'quota_exceeded' if exc.code == 429 else 'provider_error'
        if exc.code in (401, 403):
            code = 'provider_access_denied'
        raise MapsError(code, f'Google Routes returned HTTP {exc.code}; check the API key, Routes enablement, billing and quota. No route estimate was verified.') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise MapsError('provider_unavailable', 'Google Routes could not be reached within the request timeout. No route estimate was verified.') from None
    if len(raw) > MAX_RESPONSE:
        raise MapsError('invalid_response', 'Google Routes response exceeded the size limit.')
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise MapsError('invalid_response', 'Google Routes returned an unreadable response.') from None


def compute_route(owner, origin, destination, departure_time=None, *, now=None, transport=None):
    if not owner_enabled(owner):
        raise MapsError('not_configured', 'Google Routes is not enabled for this owner. Configure GOOGLE_MAPS_API_KEY and KYREX_MAPS_ROUTE_OWNERS on the server; do not guess a travel time.')
    origin = _address(origin, 'Origin')
    destination = _address(destination, 'Destination')
    now = now or datetime.now(timezone.utc)
    departure, explicit = _departure(departure_time, now)
    payload = {'origin': {'address': origin}, 'destination': {'address': destination},
               'travelMode': 'DRIVE', 'routingPreference': 'TRAFFIC_AWARE_OPTIMAL',
               'computeAlternativeRoutes': False, 'languageCode': 'en-US', 'units': 'IMPERIAL'}
    if explicit:
        payload['departureTime'] = explicit
    data = (transport or _request)(payload, os.environ['GOOGLE_MAPS_API_KEY'].strip())
    if not isinstance(data, dict) or data.get('error'):
        raise MapsError('invalid_response', 'Google Routes returned an invalid route response.')
    if data.get('fallbackInfo'):
        raise MapsError('routing_fallback', 'Google could not fulfill the requested traffic routing preference; a traffic-aware estimate was not verified.')
    geo = data.get('geocodingResults') or {}
    waypoints = {}
    area_based = []
    for side in ('origin', 'destination'):
        waypoint = geo.get(side) if isinstance(geo, dict) else None
        if not isinstance(waypoint, dict) or not isinstance(waypoint.get('placeId'), str) or not waypoint['placeId']:
            raise MapsError('location_unresolved', f'Google did not verify the {side}; provide its full address or city/state.')
        status = waypoint.get('geocoderStatus') or {}
        if not isinstance(status, dict):
            raise MapsError('invalid_response', 'Google returned invalid location verification metadata.')
        if waypoint.get('partialMatch') or status.get('code', 0):
            raise MapsError('location_ambiguous', f'Google could not exactly match the {side}; confirm a more specific address.')
        if re.search(r'[^A-Za-z0-9_-]', waypoint['placeId']) or len(waypoint['placeId']) > 300:
            raise MapsError('invalid_response', 'Google returned an invalid location identifier.')
        waypoints[side] = waypoint['placeId']
        types = waypoint.get('type') or []
        if not isinstance(types, list):
            raise MapsError('invalid_response', 'Google returned invalid location types.')
        if any(v == 'locality' or str(v).startswith('administrative_area_level_') for v in types):
            area_based.append(side)
    routes = data.get('routes')
    if not isinstance(routes, list) or not routes:
        raise MapsError('no_route', 'Google found no driving route between these locations.')
    route = routes[0]
    if not isinstance(route, dict):
        raise MapsError('invalid_response', 'Google returned an invalid route.')
    distance = route.get('distanceMeters', 0)
    duration = route.get('duration')
    match = re.fullmatch(r'(\d+(?:\.\d{1,9})?)s', duration) if isinstance(duration, str) else None
    if type(distance) is not int or distance < 0 or not match:
        raise MapsError('invalid_response', 'Google did not return valid driving distance and duration.')
    seconds = float(match[1])
    if 'distanceMeters' not in route and seconds > 0:
        raise MapsError('invalid_response', 'Google did not return a driving distance.')
    if not math.isfinite(seconds) or seconds > 365 * 86400:
        raise MapsError('invalid_response', 'Google returned an invalid driving duration.')
    link_params = {'api': 1, 'origin': origin,
        'destination': destination, 'origin_place_id': waypoints['origin'],
        'destination_place_id': waypoints['destination'], 'travelmode': 'driving'}
    link = 'https://www.google.com/maps/dir/?' + urlencode(link_params)
    if len(link) > 2048:
        # Keep the resolved endpoint IDs and shorten only display/fallback
        # labels to honor Maps URLs' documented length limit.
        link_params.update(origin=origin[:40], destination=destination[:40])
        link = 'https://www.google.com/maps/dir/?' + urlencode(link_params)
    return {'source': 'Google Maps Routes API', 'travel_mode': 'driving', 'origin': origin,
            'destination': destination, 'distance_meters': distance,
            'distance_miles': round(distance / 1609.344, 1), 'duration_seconds': seconds,
            'duration_minutes': math.ceil(seconds / 60), 'traffic_aware': True,
            'queried_at': now.isoformat(), 'departure_time': departure.isoformat(),
            'estimated_arrival': (departure + timedelta(seconds=seconds)).isoformat(),
            'directions_url': link, 'area_based_waypoints': area_based,
            'caveat': ('Route uses an area-level ' + ', '.join(area_based) + '; it is not a precise door-to-door route.'
                       if area_based else 'Google estimates driving time; traffic can change. Parking and walking time are not included.'),
            'link_note': 'Opens these endpoints in Google Maps; Maps may recompute the route and traffic.'}
