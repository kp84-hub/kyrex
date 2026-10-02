#!/usr/bin/env python3
"""Manual, read-only Termux SMS bridge. No third-party Python dependencies."""
import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

CONFIG = Path.home() / '.kyrex-messages.json'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Cloud redirects are not allowed; use its final HTTPS address')


def cloud_url(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Use the Cloud HTTPS origin, such as https://your-kyrex.example')
    return value.rstrip('/')


def post(base, path, data, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(cloud_url(base) + '/api/connections/messages/' + path,
                                 json.dumps(data).encode(), headers, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Do not echo response contents, which could contain private data.
        raise ValueError(f'Cloud rejected the request ({exc.code}); check pairing and connection status') from None


def save(data):
    fd, filename = tempfile.mkstemp(prefix='.kyrex-messages-', dir=CONFIG.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as file:
            json.dump(data, file)
        os.replace(filename, CONFIG)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)


def sms():
    result = subprocess.run(['termux-sms-list', '-t', 'inbox', '-l', '100', '-n', '-d'],
                            capture_output=True, text=True, timeout=60, check=True)
    messages = json.loads(result.stdout)
    if not isinstance(messages, list) or len(messages) > 100:
        raise ValueError('Unexpected phone SMS response')
    if any(not isinstance(m, dict) for m in messages):
        raise ValueError('Unexpected phone SMS response')
    # Termux returns oldest first within its bounded recent snapshot.
    messages.sort(key=lambda m: str(m.get('received', '')), reverse=True)
    return [{k: str(m.get(k) or '')[:limit] for k, limit in
             [('sender', 200), ('number', 100), ('received', 100), ('body', 10000)]}
            for m in messages]


def main():
    parser = argparse.ArgumentParser(description='Read-only Android SMS snapshot bridge')
    parser.add_argument('command', choices=['pair', 'sync', 'forget'])
    parser.add_argument('--cloud', help='Your Kyrex Cloud HTTPS origin (pair only)')
    args = parser.parse_args()
    try:
        if args.command == 'forget':
            CONFIG.unlink(missing_ok=True)
            print('Local credential removed. Also disconnect Messages in Kyrex to revoke access and delete the snapshot.')
        elif args.command == 'pair':
            base = cloud_url(args.cloud or input('Kyrex Cloud HTTPS address: ').strip())
            code = getpass.getpass('Pairing code from Kyrex (hidden): ').strip()
            paired = post(base, 'pair', {'pairing_code': code})
            save({'cloud': base, 'upload_token': paired['upload_token']})
            print('Phone paired. Run: python messages_phone.py sync')
        else:
            config = json.loads(CONFIG.read_text())
            messages = sms()
            result = post(config['cloud'], 'sync', {'messages': messages}, config['upload_token'])
            print(f"Synced {result['count']} recent received SMS messages. Run sync again to refresh; RCS is not included.")
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, urllib.error.URLError):
        print('Messages setup/sync failed. Check your Cloud address, pairing code, Termux:API installation, and SMS permission. No credential or message contents were printed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
