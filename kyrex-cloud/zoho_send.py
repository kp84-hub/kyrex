"""Zoho US HTTPS sender: owner OAuth, encrypted grants, one submission only."""
import hashlib
import json
import os
import re
import secrets
import email_send
import time
from email import policy
from email.parser import Parser
from email.utils import getaddresses
from urllib.parse import urlencode, urlsplit

import requests
import connectors as core
import agent_inbox

AUTH = 'https://accounts.zoho.com/oauth/v2/'
API = 'https://mail.zoho.com/api/'
SCOPES = ('ZohoMail.accounts.READ', 'ZohoMail.messages.CREATE', 'ZohoMail.messages.READ')
MAX_RESPONSE = 262144


def config():
    values = [os.environ.get('ZOHO_CLIENT_ID', ''), os.environ.get('ZOHO_CLIENT_SECRET', ''),
              os.environ.get('ZOHO_REDIRECT_URI', '')]
    target = urlsplit(values[2])
    if (not all(values) or target.scheme != 'https' or not target.netloc or target.username
            or target.query or target.fragment or target.path != '/api/connections/zoho/callback'):
        raise core.ConnectorConfigError('Zoho sending needs a server OAuth app. Set ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET and ZOHO_REDIRECT_URI on Kyrex Cloud; then authorize sending here.')
    return dict(zip(('client_id', 'client_secret', 'redirect_uri'), values))


def http(method, url, *, token=None, data=None, body=None, params=None):
    # URLs are constructed from fixed Zoho US origins and validated numeric IDs.
    with requests.request(method, url, headers={'Accept': 'application/json',
            **({'Authorization': 'Zoho-oauthtoken ' + token} if token else {})},
            data=data, json=body, params=params, timeout=(5, 20), allow_redirects=False,
            stream=True) as response:
        raw = bytearray()
        deadline = time.monotonic() + 40
        for chunk in response.iter_content(16384):
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE or time.monotonic() > deadline:
                raise core.ConnectorUnavailable('Zoho response exceeded the safe size limit.')
        try:
            value = json.loads(raw)
        except ValueError:
            value = {}
        return response.status_code, value


def identifier(value):
    value = str(value or '')
    if not re.fullmatch(r'[0-9]{1,30}', value):
        raise core.ConnectorUnavailable('Zoho did not return a valid email identity.')
    return value


def result(response):
    status, value = response
    if (status != 200 or not isinstance(value, dict)
            or value.get('status', {}).get('code') != 200):
        raise core.ConnectorUnavailable('Zoho authorization or email access failed. Reauthorize sending in Connections.')
    return value.get('data')


def headers(raw):
    if not isinstance(raw, str) or len(raw.encode()) > 65536:
        raise core.ConnectorUnavailable('Zoho did not return bounded email headers.')
    msg = Parser(policy=policy.default).parsestr(raw.split('\r\n\r\n', 1)[0].split('\n\n', 1)[0], headersonly=True)
    if any(len(msg.get_all(name, [])) != 1 for name in ('From', 'Message-ID')):
        raise core.ConnectorUnavailable('Zoho returned ambiguous email headers.')
    return msg


class SendStore:
    def __init__(self, inbox=None, request=None):
        self.inbox = inbox or agent_inbox.default_store()
        self.request = request or http
        with self.inbox.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS zoho_send_grants (owner TEXT PRIMARY KEY, revision TEXT NOT NULL, sealed TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS zoho_send_states (state TEXT PRIMARY KEY, owner TEXT NOT NULL, revision TEXT NOT NULL, redirect TEXT NOT NULL, created REAL NOT NULL)')

    def view(self, owner):
        row = self.inbox.record(owner)
        with self.inbox.db() as db:
            grant = db.execute('SELECT revision FROM zoho_send_grants WHERE owner=?', (owner,)).fetchone()
        try:
            cfg = config()
        except core.ConnectorError:
            cfg = {}
        connected = bool(cfg and grant and grant[0] == row['revision'] and row.get('source') == 'zoho' and row.get('sealed'))
        return {'configured': bool(cfg), 'connected': connected, 'transport': 'https',
                'redirect_uri': cfg.get('redirect_uri', ''), 'review_required': True}

    def begin(self, owner):
        from email_send import mailbox
        row = mailbox(self.inbox, owner)
        cfg = config()
        state = secrets.token_urlsafe(32)
        with self.inbox.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM zoho_send_states WHERE owner=? OR created<?', (owner, time.time() - 600))
            db.execute('INSERT INTO zoho_send_states VALUES (?,?,?,?,?)',
                (hashlib.sha256(state.encode()).hexdigest(), owner, row['revision'], cfg['redirect_uri'], time.time()))
        return {'authorization_url': AUTH + 'auth?' + urlencode({'client_id': cfg['client_id'],
            'response_type': 'code', 'redirect_uri': cfg['redirect_uri'], 'scope': ','.join(SCOPES),
            'access_type': 'offline', 'prompt': 'consent', 'state': state})}

    def complete(self, owner, state, code):
        cfg = config()
        if not isinstance(state, str) or not 20 <= len(state) <= 100 or not isinstance(code, str) or not 1 <= len(code) <= 2048:
            raise core.OAuthStateError('Invalid Zoho authorization. Start again in Connections.')
        with self.inbox.db() as db:
            db.execute('BEGIN IMMEDIATE')
            key = hashlib.sha256(state.encode()).hexdigest()
            pending = db.execute('SELECT owner,revision,redirect,created FROM zoho_send_states WHERE state=?', (key,)).fetchone()
            if not pending or pending[0] != owner or pending[2] != cfg['redirect_uri'] or time.time() - pending[3] > 600:
                raise core.OAuthStateError('Zoho authorization expired or belongs to another account. Start again in Connections.')
            db.execute('DELETE FROM zoho_send_states WHERE state=?', (key,))
        # State is consumed even if the exchange fails; codes never persist.
        with self.inbox.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.inbox.record(owner)
            if row['revision'] != pending[1] or row.get('source') != 'zoho' or not row.get('sealed'):
                raise core.ConnectorUnavailable('The dedicated mailbox changed. Authorize it again.')
            status, tokens = self.request('POST', AUTH + 'token', data={**cfg, 'code': code, 'grant_type': 'authorization_code'})
            tokens = self._tokens(status, tokens, require_refresh=True)
            accounts = result(self.request('GET', API + 'accounts', token=tokens['access_token']))
            matches = [a for a in accounts if isinstance(a, dict) and a.get('type') == 'ZOHO_ACCOUNT'
                       and str(a.get('primaryEmailAddress', '')).lower() == row['address'].lower()
                       and a.get('mailboxStatus') == 'enabled'] if isinstance(accounts, list) else []
            if len(matches) != 1:
                raise core.ConnectorUnavailable('Sign in with the dedicated Zoho mailbox account shown in Connections, not your administrator or another mailbox.')
            tokens['account_id'] = identifier(matches[0].get('accountId'))
            result(self.request('GET', API + 'accounts/' + tokens['account_id'] + '/messages/search',
                token=tokens['access_token'], params={'searchKey': 'in:Inbox', 'limit': 1, 'start': 1}))
            db.execute('INSERT OR REPLACE INTO zoho_send_grants VALUES (?,?,?)',
                       (owner, row['revision'], core.seal_tokens(tokens)))
        return self.view(owner)

    def _tokens(self, status, value, require_refresh=False):
        if (status != 200 or not isinstance(value, dict) or value.get('error')
                or not isinstance(value.get('access_token'), str) or not 1 <= len(value['access_token']) <= 4096
                or any(not 33 <= ord(c) <= 126 for c in value['access_token'])
                or (require_refresh and (not isinstance(value.get('refresh_token'), str)
                    or not 1 <= len(value['refresh_token']) <= 4096
                    or any(not 33 <= ord(c) <= 126 for c in value['refresh_token'])))):
            raise core.ConnectorUnavailable('Zoho sending authorization failed. Authorize the dedicated account again in Connections.')
        # Do not follow api_domain or accounts-server returned by the provider/browser.
        return {'access_token': value['access_token'], 'refresh_token': value.get('refresh_token', ''),
                'expires_at': time.time() + min(3600, max(0, int(value.get('expires_in', 0))))}

    def disconnect(self, owner):
        with self.inbox.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM zoho_send_grants WHERE owner=?', (owner,))
            db.execute('DELETE FROM zoho_send_states WHERE owner=?', (owner,))
        return self.view(owner)

    def _grant(self, db, owner, payload):
        from email_send import same_mailbox
        if not same_mailbox(self.inbox.record(owner), payload):
            raise core.ConnectorUnavailable('The mailbox changed. Prepare a new draft.')
        grant = db.execute('SELECT revision,sealed FROM zoho_send_grants WHERE owner=?', (owner,)).fetchone()
        if not grant or grant[0] != payload['source_revision']:
            raise core.ConnectorUnavailable('Enable Zoho sending in Connections → Email inbox before sending. Nothing was submitted.')
        tokens = core.unseal_tokens(grant[1])
        identifier(tokens.get('account_id'))
        if tokens.get('expires_at', 0) <= time.time() + 60:
            cfg = config()
            status, refreshed = self.request('POST', AUTH + 'token', data={'client_id': cfg['client_id'],
                'client_secret': cfg['client_secret'], 'refresh_token': tokens['refresh_token'], 'grant_type': 'refresh_token'})
            new = self._tokens(status, refreshed)
            new['refresh_token'] = new['refresh_token'] or tokens['refresh_token']
            tokens.update(new)
            db.execute('UPDATE zoho_send_grants SET sealed=? WHERE owner=?', (core.seal_tokens(tokens), owner))
        return tokens

    def _parent(self, tokens, payload):
        # API IDs differ from RFC Message-ID/IMAP UID. Find bounded Inbox candidates,
        # then verify the exact host-derived Message-ID before choosing a reply endpoint.
        sender = getaddresses([payload['original_sender']])
        from email_send import address
        if len(sender) != 1:
            raise core.ConnectorUnavailable('Cannot identify this reply parent. Select the email again.')
        origin = address(sender[0][1])
        base = API + 'accounts/' + tokens['account_id']
        found = result(self.request('GET', base + '/messages/search', token=tokens['access_token'],
            params={'searchKey': 'in:Inbox::sender:' + origin, 'limit': 10, 'start': 1,
                    'receivedTime': int(time.time() * 1000)}))
        if not isinstance(found, list) or len(found) > 10:
            raise core.ConnectorUnavailable('Cannot verify the selected reply parent.')
        deadline = time.monotonic() + 45
        for item in found:
            if time.monotonic() > deadline:
                break
            mid, folder = identifier(item.get('messageId')), identifier(item.get('folderId'))
            value = result(self.request('GET', base + f'/folders/{folder}/messages/{mid}/header', token=tokens['access_token']))
            msg = headers(value.get('headerContent'))
            if (str(msg['Message-ID']).strip() == payload['in_reply_to']
                    and [v[1].lower() for v in getaddresses([str(msg['From'])])] == [origin.lower()]
                    and len(msg.get_all('Reply-To', [])) <= 1
                    and [v[1].lower() for v in getaddresses([str(msg.get('Reply-To') or msg['From'])])] == [payload['to'].lower()]):
                return mid
        raise core.ConnectorUnavailable('The selected email was not found among the latest 10 Inbox messages from its sender. Select a recent email or create a new email; nothing was sent.')

    def send(self, owner, payload):
        submitted = False
        payload['transport'] = 'zoho_https'
        try:
            with self.inbox.db() as db:
                db.execute('BEGIN IMMEDIATE')  # Source changes/disconnect cannot race submission.
                email_send.content(payload['to'], payload['subject'], payload['body'])
                tokens = self._grant(db, owner, payload)
                path = API + 'accounts/' + tokens['account_id'] + '/messages'
                body = {'fromAddress': payload['from'], 'toAddress': payload['to'], 'subject': payload['subject'],
                        'content': payload['body'], 'mailFormat': 'plaintext', 'encoding': 'UTF-8', 'askReceipt': 'no'}
                if payload.get('parent_id'):
                    path += '/' + self._parent(tokens, payload)
                    body['action'] = 'reply'
                submitted = True  # Any interruption after starting POST is uncertain; never retry.
                status, value = self.request('POST', path, token=tokens['access_token'], body=body)
                if 400 <= status < 500:
                    return 'failed', 'Zoho rejected this email. Check sending authorization and the recipient. No automatic retry.'
                if status != 200 or not isinstance(value, dict) or value.get('status', {}).get('code') != 200:
                    return 'unknown', 'Zoho submission could not be confirmed. Check Zoho Sent before creating another send. No automatic retry.'
                data = value.get('data') or {}
                if isinstance(data, dict) and re.fullmatch(r'[0-9]{1,30}', str(data.get('messageId', ''))):
                    payload['zoho_message_id'] = str(data['messageId'])
                from email_send import MESSAGE_ID
                mail_id = data.get('mailId') if isinstance(data, dict) else None
                if isinstance(mail_id, str) and len(mail_id) <= 254 and MESSAGE_ID.fullmatch(mail_id):
                    payload['zoho_mail_id'] = mail_id
                return 'accepted', 'Zoho accepted this email. Delivery and appointment confirmation are not yet verified.'
        except Exception as exc:
            if submitted:
                return 'unknown', 'The send outcome is unknown. Check Zoho Sent before creating another send. No automatic retry.'
            return 'failed', str(exc) if isinstance(exc, core.ConnectorError) else 'Zoho sending is unavailable. Check authorization in Connections. Nothing was submitted.'

    def thread_id(self, owner, payload):
        from email_send import MESSAGE_ID
        if payload.get('zoho_mail_id'):
            if MESSAGE_ID.fullmatch(payload['zoho_mail_id']) and len(payload['zoho_mail_id']) <= 254:
                return payload['zoho_mail_id']
            raise core.ConnectorUnavailable('Invalid sent-message identity.')
        if not payload.get('zoho_message_id'):
            raise core.ConnectorUnavailable('Zoho did not return a sent-message identity. Check replies in Zoho; Kyrex will not guess a thread or resend.')
        with self.inbox.db() as db:
            db.execute('BEGIN IMMEDIATE')
            tokens = self._grant(db, owner, payload)
            value = result(self.request('GET', API + 'accounts/' + tokens['account_id'] + '/messages/'
                + identifier(payload['zoho_message_id']) + '/originalmessage', token=tokens['access_token']))
            msg = headers(value.get('content'))
            mid = str(msg['Message-ID']).strip()
            if (not MESSAGE_ID.fullmatch(mid) or len(mid) > 254
                    or [v[1].lower() for v in getaddresses([str(msg['From'])])] != [payload['from'].lower()]):
                raise core.ConnectorUnavailable('Zoho sent-message identity could not be verified. Check replies in Zoho.')
            return mid


def send(inbox, owner, payload):
    return SendStore(inbox).send(owner, payload)
