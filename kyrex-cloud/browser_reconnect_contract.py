"""Private browser reconnect protocol. No passwords, cookies or screen transport."""
import re
from urllib.parse import urlsplit

REQUEST = re.compile(r'^[a-f0-9]{32}$')
IDENTITY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}$')
ACTIONS = {'start', 'status', 'end'}
STATES = {'idle', 'starting', 'ready', 'busy', 'stopping', 'ended', 'unavailable', 'pending'}


def viewer_url(value):
    """Only an operator-configured private Tailscale HTTPS viewer can be linked."""
    if not isinstance(value, str): return ''
    try:
        p = urlsplit(value)
        if (p.scheme != 'https' or not p.hostname or not p.hostname.endswith('.ts.net')
                or p.username or p.password or p.port not in (None, 443)
                or p.query or p.fragment or p.path not in ('', '/', '/vnc.html')):
            return ''
    except ValueError: return ''
    return value.rstrip('/') + ('/vnc.html' if p.path in ('', '/') else '')


def command(value):
    if not isinstance(value, dict) or set(value) != {'request_id', 'owner', 'bot_id', 'action', 'lease', 'url'}:
        raise ValueError('Invalid browser reconnect request.')
    if (not REQUEST.fullmatch(str(value['request_id'])) or value['action'] not in ACTIONS
            or not all(isinstance(value[k], str) and IDENTITY.fullmatch(value[k]) for k in ('owner', 'bot_id'))
            or value['lease'] and not REQUEST.fullmatch(str(value['lease']))):
        raise ValueError('Invalid browser reconnect request.')
    if value['action'] == 'start':
        import browser_diagnostics
        browser_diagnostics.target(value['url'])
        if value['lease']: raise ValueError('A new sign-in request cannot select a previous lease.')
    elif value['url'] or value['action'] == 'end' and not value['lease']:
        raise ValueError('Choose the active private sign-in window.')
    return dict(value)


def public_result(value):
    """Project only fixed state, lease and a validated private URL."""
    value = value if isinstance(value, dict) else {}
    lease = value.get('lease', '')
    state = value.get('state')
    return {'state': state if isinstance(state, str) and state in STATES else 'unavailable',
            'lease': lease if isinstance(lease, str) and REQUEST.fullmatch(lease) else '',
            'viewer_url': viewer_url(value.get('viewer_url', ''))}
