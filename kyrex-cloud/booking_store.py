"""Encrypted owner/conversation booking state and single-use browser authority."""
import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import booking_schema as schema
import workout_booking_schema as workout
import connectors
from paths import data_dir

class BookingError(Exception): pass


class BookingStore:
    def __init__(self, path=None):
        self.path=Path(path) if path else data_dir()/'bookings.sqlite3'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS bookings (id TEXT PRIMARY KEY,owner TEXT NOT NULL,conversation TEXT NOT NULL,revision TEXT NOT NULL,state TEXT NOT NULL,sealed TEXT NOT NULL,updated REAL NOT NULL,request_key TEXT)')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS booking_request ON bookings(owner,request_key) WHERE request_key IS NOT NULL')
            db.execute('CREATE TABLE IF NOT EXISTS booking_wires (nonce TEXT PRIMARY KEY,id TEXT NOT NULL,owner TEXT NOT NULL,bot TEXT NOT NULL,host TEXT NOT NULL,sha TEXT NOT NULL,stage TEXT NOT NULL,created REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0)')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def get(self, owner, ident):
        with self.db() as db: row=db.execute('SELECT * FROM bookings WHERE owner=? AND id=?',(owner,ident)).fetchone()
        if not row: raise BookingError('Booking not found in your account.')
        value=dict(row);value['data']=connectors.unseal_tokens(value.pop('sealed'))
        return value

    def create(self, owner, conversation, request_key=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if request_key:
                old=db.execute('SELECT id FROM bookings WHERE owner=? AND request_key=?',(owner,request_key)).fetchone()
                if old: return self.get(owner,old['id'])
            ident=uuid.uuid4().hex
            db.execute('INSERT INTO bookings VALUES (?,?,?,?,?,?,?,?)',(ident,owner,conversation,uuid.uuid4().hex,'collecting',connectors.seal_tokens({'details':{},'messages':[],'steps':[]}),time.time(),request_key))
        return self.get(owner,ident)

    def update(self, row, data, state):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed=db.execute('UPDATE bookings SET sealed=?,state=?,revision=?,updated=? WHERE owner=? AND id=? AND revision=?',
                (connectors.seal_tokens(data),state,uuid.uuid4().hex,time.time(),row['owner'],row['id'],row['revision'])).rowcount
            if changed!=1: raise BookingError('This booking changed in another request. Check the latest proposal.')
        return self.get(row['owner'],row['id'])

    def claim(self, owner, ident, revision, conversation, assistant_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM bookings WHERE owner=? AND id=?',(owner,ident)).fetchone()
            if not row or row['conversation']!=conversation or row['revision']!=revision:
                raise BookingError('That booking proposal changed. Review the latest details before saying yes.')
            if row['state']!='proposed': raise BookingError('This booking has already been answered or submitted. It will not be submitted again.')
            data=connectors.unseal_tokens(row['sealed'])
            if time.time()-row['updated']>600 or data.get('assistant_id')!=assistant_id:
                raise BookingError('The booking review expired. Ask me to check availability again.')
            db.execute('UPDATE bookings SET state=?,updated=? WHERE id=?',('submitting',time.time(),ident))
        return self.get(owner,ident)

    def wire(self, row, bot, host, stage):
        data=row['data']
        if stage=='submit' and row['state']!='submitting': raise BookingError('Only your confirmed proposal may be submitted.')
        if stage=='prepare' and row['state']!='preparing': raise BookingError('Booking preparation is not active.')
        if stage=='verify' and (data.get('kind')!='workout' or row['state']!='submitting' or data.get('cancel_requested')):
            raise BookingError('Only an existing workout submission can be verified.')
        nonce=secrets.token_hex(32)
        spec={'id':row['id'],'nonce':nonce,'stage':stage,'url':data['url'],'details':data['details'],'steps':data.get('steps',[])}
        if stage=='submit': spec.update(expected=data['expected'],submit=data['submit'])
        action = workout.ACTION if data.get('kind') == 'workout' else schema.ACTION
        if action == workout.ACTION:
            if stage in {'submit', 'verify'}: spec['request'] = data['request']
            if stage == 'verify': spec['steps'] = []
            workout.task(spec)
        else:
            schema.task(spec)
        text=json.dumps({action:spec},sort_keys=True)
        with self.db() as db:
            db.execute('INSERT INTO booking_wires (nonce,id,owner,bot,host,sha,stage,created) VALUES (?,?,?,?,?,?,?,?)',
                (hashlib.sha256(nonce.encode()).hexdigest(),row['id'],row['owner'],bot,host,hashlib.sha256(text.encode()).hexdigest(),stage,time.time()))
        return text

    def authorize(self, owner, bot, host, text):
        # Only immutable server-issued tasks get a task-local booking operation.
        try:
            action,spec=workout.wire(text)
        except Exception: raise BookingError('Invalid booking browser authority.') from None
        key=hashlib.sha256(spec['nonce'].encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            wire=db.execute('SELECT * FROM booking_wires WHERE nonce=?',(key,)).fetchone()
            current=db.execute('SELECT state,sealed FROM bookings WHERE owner=? AND id=?',(owner,spec['id'])).fetchone()
            if (not wire or wire['owner']!=owner or wire['bot']!=bot or wire['host']!=host or wire['used']
                    or wire['sha']!=hashlib.sha256(text.encode()).hexdigest() or time.time()-wire['created']>120
                    or not current or current['state']!=('submitting' if spec['stage'] in {'submit', 'verify'} else 'preparing')):
                raise BookingError('This booking browser request is stale, foreign or already used.')
            import bots
            import browser_hosts
            import serve
            record = bots.get_bot(bot)
            bound = browser_hosts.host_for(owner, bot)
            saved = connectors.unseal_tokens(current['sealed']).get('browser_binding', {})
            if spec['stage'] == 'verify' and connectors.unseal_tokens(current['sealed']).get('cancel_requested'):
                raise BookingError('Booking verification was stopped.')
            if (record.get('owner') != owner or record.get('status') != 'running' or not serve.browser_bot_granted(record)
                    or not bound or bound.host_id != host or not bound.is_available()
                    or saved != {'bot':bot,'host':host,'allowlist':record.get('browser_allowlist')}):
                raise BookingError('The booking Browser Bot or host changed.')
            db.execute('UPDATE booking_wires SET used=1 WHERE nonce=?',(key,))
        return True


    def request_cancel(self, owner, ident):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT sealed,state FROM bookings WHERE owner=? AND id=?',(owner,ident)).fetchone()
            if row and row['state'] in {'preparing','submitting'}:
                data=connectors.unseal_tokens(row['sealed']);data['cancel_requested']=True
                db.execute('UPDATE bookings SET sealed=? WHERE owner=? AND id=?',(connectors.seal_tokens(data),owner,ident))

    def active(self, owner, bot, host, text):
        try:
            action,spec=workout.wire(text)
            key=hashlib.sha256(spec['nonce'].encode()).hexdigest()
            row=self.get(owner,spec['id'])
            with self.db() as db: wire=db.execute('SELECT * FROM booking_wires WHERE nonce=?',(key,)).fetchone()
            import bots
            import browser_hosts
            import serve
            live=bots.get_bot(bot);bound=browser_hosts.host_for(owner,bot)
            return bool(wire and wire['used']==1 and wire['owner']==owner and wire['bot']==bot and wire['host']==host
                and wire['sha']==hashlib.sha256(text.encode()).hexdigest()
                and row['state']==('submitting' if spec['stage'] in {'submit', 'verify'} else 'preparing')
                and not row['data'].get('cancel_requested')
                and live.get('owner')==owner and live.get('status')=='running' and serve.browser_bot_granted(live)
                and bound and bound.host_id==host and bound.is_available()
                and row['data'].get('browser_binding')=={'bot':bot,'host':host,'allowlist':live.get('browser_allowlist')})
        except Exception: return False
