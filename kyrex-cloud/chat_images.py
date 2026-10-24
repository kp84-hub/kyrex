"""Private, conversation-bound image attachments on the existing data volume.

Only opaque IDs leave this store. No remote URL fetching, original filenames,
EXIF, or image bytes in transcripts/task text. Images expire after 30 days and
are removed when their conversation is deleted.
"""
import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import time
import uuid
import warnings
from contextlib import contextmanager

from cryptography.fernet import Fernet, InvalidToken
from paths import data_dir

MAX_UPLOAD = 5 * 1024 * 1024
MAX_IMAGES = 3
OWNER_QUOTA = 60 * 1024 * 1024
RETENTION = 30 * 86400
ID = re.compile(r'^[a-f0-9]{32}$')


class ImageError(ValueError):
    pass


def _box():
    secret = os.environ.get('WEB_SESSION_SECRET') or os.environ.get('KYREX_PROVIDER_SECRETS_KEY')
    if not secret:
        raise ImageError('Private image storage is not configured.')
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(('chat-images:' + secret).encode()).digest()))


@contextmanager
def database():
    path = data_dir() / 'chat_images.sqlite3'
    db = sqlite3.connect(path, timeout=15)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS images (owner TEXT, conversation TEXT, id TEXT, '
                   'created REAL, size INTEGER, value BLOB, PRIMARY KEY(owner, conversation, id))')
        db.commit()
        path.chmod(0o600)
        with db:
            yield db
    finally:
        db.close()


def validate_ids(ids):
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_IMAGES
            or any(not isinstance(i, str) or not ID.fullmatch(i) for i in ids)
            or len(set(ids)) != len(ids)):
        raise ImageError('Attach one to three images using their uploaded image IDs.')
    return ids


def _normalize(raw):
    from PIL import Image, ImageOps, UnidentifiedImageError
    if not raw or len(raw) > MAX_UPLOAD:
        raise ImageError('Each image must be 5 MB or smaller.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {'PNG', 'JPEG', 'WEBP'}:
                    raise ImageError('Choose a PNG, JPEG, or WebP image.')
                if source.width * source.height > 16_000_000:
                    raise ImageError('Image resolution must be 16 megapixels or smaller.')
                if getattr(source, 'n_frames', 1) != 1:
                    raise ImageError('Choose a still image, rather than an animation.')
                image = ImageOps.exif_transpose(source)
                image.thumbnail((2048, 2048))
                # Re-encode pixels only: discard EXIF, comments and other metadata.
                clean = Image.new('RGB', image.size, 'white')
                if image.mode in {'RGBA', 'LA'} or 'transparency' in image.info:
                    image = image.convert('RGBA')
                    clean.paste(image, mask=image.getchannel('A'))
                else:
                    clean.paste(image.convert('RGB'))
                out = io.BytesIO()
                clean.save(out, format='JPEG', quality=92)
                return out.getvalue(), clean.width, clean.height
    except ImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ImageError('This image could not be read. Choose a valid PNG, JPEG, or WebP.') from None


def save(owner, conversation, raw):
    image, width, height = _normalize(raw)
    image_id = uuid.uuid4().hex
    value = _box().encrypt(json.dumps({'owner': owner, 'conversation': conversation, 'id': image_id,
        'data': base64.b64encode(image).decode()}).encode())
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM images WHERE created < ?', (time.time() - RETENTION,))
        size = db.execute('SELECT COALESCE(SUM(size),0) FROM images WHERE owner=?', (owner,)).fetchone()[0]
        if size + len(image) > OWNER_QUOTA:
            raise ImageError('Image storage is full. Delete an older conversation with images first.')
        db.execute('INSERT INTO images VALUES (?,?,?,?,?,?)',
                   (owner, conversation, image_id, time.time(), len(image), value))
    return {'id': image_id, 'url': url(conversation, image_id), 'width': width, 'height': height}


def url(conversation, image_id):
    # Conversation IDs come from the server, but validate again before URL construction.
    if not ID.fullmatch(str(conversation)) or not ID.fullmatch(str(image_id)):
        raise ImageError('Image reference is invalid.')
    return f'/api/conversations/{conversation}/images/{image_id}'


def read(owner, conversation, image_id):
    validate_ids([image_id])
    with database() as db:
        row = db.execute('SELECT value FROM images WHERE owner=? AND conversation=? AND id=? AND created>=?',
                         (owner, conversation, image_id, time.time() - RETENTION)).fetchone()
    if not row:
        raise ImageError('Image is unavailable or expired. Upload it again.')
    try:
        value = json.loads(_box().decrypt(row[0]))
        if (value.get('owner'), value.get('conversation'), value.get('id')) != (owner, conversation, image_id):
            raise ValueError()
        return base64.b64decode(value['data'], validate=True)
    except (InvalidToken, ValueError, KeyError, TypeError):
        raise ImageError('Image could not be opened. Upload it again.') from None


def references(owner, conversation, ids):
    return [{'id': i, 'url': url(conversation, i)} for i in validate_ids(ids)
            if read(owner, conversation, i)]


def delete_conversation(owner, conversation):
    with database() as db:
        db.execute('DELETE FROM images WHERE owner=? AND conversation=?', (owner, conversation))
