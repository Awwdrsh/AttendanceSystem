"""
Encrypted file store (port of apps/api/src/modules/files/store.ts). Files live in stored_file.data_enc, encrypted with
the field cipher (AAD `stored_file:<company>:<id>`, base64 of the bytes as plaintext, so either stack reads the other's
files). Only real PDFs and images are accepted: the declared type must match the file's magic bytes.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

from django.db import transaction
from django.db.models.functions import Coalesce, Now
from django.http import HttpResponse

from hajiri.core.clock import now as _now
from hajiri.core.crypto import field_cipher
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError
from hajiri.core.scoping import as_uuid
from hajiri.db.models import StoredFile

FILE_MAX_BYTES = 4 * 1024 * 1024
DOCUMENT_MIMES = ('application/pdf', 'image/jpeg', 'image/png', 'image/webp')
IMAGE_MIMES = ('image/jpeg', 'image/png', 'image/webp')

_SIGNATURES = {
    'application/pdf': lambda b: len(b) > 5 and b[:5] == b'%PDF-',
    'image/jpeg': lambda b: len(b) > 3 and b[0] == 0xFF and b[1] == 0xD8 and b[2] == 0xFF,
    'image/png': lambda b: len(b) > 8 and b[:8] == b'\x89PNG\r\n\x1a\n',
    'image/webp': lambda b: len(b) > 12 and b[:4] == b'RIFF' and b[8:12] == b'WEBP',
}



def has_signature(mime: str, data: bytes) -> bool:
    """Whether the bytes start the way a file of that type does (its magic number); False for other types."""
    check = _SIGNATURES.get(mime)
    return bool(check and check(data))


_FIELDS = ('id', 'company_id', 'purpose', 'filename', 'mime', 'bytes', 'sha256', 'uploaded_by_id', 'attached_to',
           'deleted_at', 'created_at')


@dataclass(frozen=True)
class ParsedFile:
    mime: str
    data: bytes


def _allowed_text(allowed) -> str:
    return ', '.join('a PDF' if a == 'application/pdf' else a.replace('image/', '').upper() for a in allowed)


def check_file(mime: str, data: bytes, *, allowed=DOCUMENT_MIMES, max_bytes: int | None = None) -> ParsedFile:
    """Check type, size and signature of raw bytes (e.g. a multipart upload)."""
    limit = min(max_bytes or FILE_MAX_BYTES, FILE_MAX_BYTES)
    if mime not in allowed:
        raise BadRequestError(f'The file must be {_allowed_text(allowed)}.')
    if len(data) == 0:
        raise BadRequestError('The file is empty.')
    if len(data) > limit:
        mb = round(limit / 1024 / 1024 * 10) / 10
        raise BadRequestError(f'The file is too large (max {mb:g} MB).')
    if not _SIGNATURES[mime](data):
        raise BadRequestError('The file could not be read. Check that it is not damaged.')
    return ParsedFile(mime, data)


def parse_upload(data_url: str, *, allowed=DOCUMENT_MIMES, max_bytes: int | None = None) -> ParsedFile:
    """Decode a `data:<mime>;base64,` upload, checking the allowed types, the signature and the size."""
    m = re.fullmatch(r'data:([a-z]+/[a-z0-9.+-]+);base64,([A-Za-z0-9+/]+={0,2})', data_url.strip())
    if not m or m[1] not in allowed:
        raise BadRequestError(f'The file must be {_allowed_text(allowed)}.')
    try:
        data = base64.b64decode(m[2] + '=' * (-len(m[2]) % 4))
    except binascii.Error:
        data = b''
    return check_file(m[1], data, allowed=allowed, max_bytes=max_bytes)


def parse_uploaded_file(f, *, allowed=DOCUMENT_MIMES, max_bytes: int | None = None) -> ParsedFile:
    """A Django UploadedFile from a multipart form. The type comes from the bytes' signature, not the browser."""
    limit = min(max_bytes or FILE_MAX_BYTES, FILE_MAX_BYTES)
    data = f.read(limit + 1)
    mime = next((m for m, sig in _SIGNATURES.items() if sig(data)), getattr(f, 'content_type', '') or '')
    return check_file(mime, data, allowed=allowed, max_bytes=max_bytes)


def clean_filename(name: str) -> str:
    """Printable and short; never trusted for anything but display."""
    s = re.sub(r'[\x00-\x1f\x7f"\\/<>:|?*]+', '_', unicodedata.normalize('NFC', name))
    s = re.sub(r'\s+', ' ', s).strip()
    return (s or 'file')[:200]


def _aad(company_id: str, file_id: str) -> str:
    return f'stored_file:{company_id}:{file_id}'


def _files(company_id: str):
    """Live (not deleted) uploads of one company."""
    return StoredFile.objects.for_company(company_id).filter(deleted_at__isnull=True)


def _row(r: dict | None) -> dict | None:
    """The row as the modules read it: ids as strings, `uploaded_by` the user id."""
    if r is None:
        return None
    r['uploaded_by'] = r.pop('uploaded_by_id', None)
    for k in ('id', 'company_id', 'uploaded_by'):
        if r.get(k) is not None:
            r[k] = str(r[k])
    return r


@transaction.atomic
def save_file(*, company_id: str, purpose: str, filename: str, file: ParsedFile, uploaded_by: str | None,
              attached_to: str | None = None) -> dict:
    # The cipher binds the bytes to the row id, which the database assigns (uuidv7): insert, then store the blob.
    f = StoredFile.objects.create(company_id=company_id, purpose=purpose, filename=clean_filename(filename), mime=file.mime,
                                  bytes=len(file.data), sha256=hashlib.sha256(file.data).hexdigest(),
                                  uploaded_by_id=uploaded_by, attached_to=attached_to,
                                  attached_at=Now() if attached_to is not None else None)
    blob = field_cipher().encrypt(base64.b64encode(file.data).decode(), _aad(str(company_id), str(f.pk)))
    StoredFile.objects.filter(pk=f.pk).update(data_enc=blob)
    return _row(StoredFile.objects.filter(pk=f.pk).values(*_FIELDS).first())


def file_meta(company_id: str, file_id: str) -> dict | None:
    return _row(_files(company_id).filter(pk=as_uuid(file_id)).values(*_FIELDS).first())


def attach_file(*, company_id: str, file_id: str, user_id: str, purpose: str | list[str] | tuple[str, ...],
                attached_to: str) -> dict:
    """Attach an upload to a record (e.g. `expense_claim:<id>`). Only the uploader, only once, only for the expected
    purpose. Call inside the module's transaction."""
    meta = file_meta(company_id, file_id)
    if not meta:
        raise BadRequestError('The attached file was not found. Upload it again.')
    purposes = [purpose] if isinstance(purpose, str) else list(purpose)
    if meta['purpose'] not in purposes:
        raise BadRequestError('This file was uploaded for something else. Upload it again.')
    if meta['attached_to'] and meta['attached_to'] != attached_to:
        raise BadRequestError('This file is already attached to another record.')
    if meta['uploaded_by'] != str(user_id):
        raise ForbiddenError('You can only attach files you uploaded.')
    StoredFile.objects.filter(pk=meta['id']).update(attached_to=attached_to, attached_at=Coalesce('attached_at', Now()))
    return {**meta, 'attached_to': attached_to}


def read_file(company_id: str, file_id: str) -> dict:
    r = _files(company_id).filter(pk=as_uuid(file_id)).values(*_FIELDS, 'data_enc').first()
    if not r or r['data_enc'] is None:
        raise NotFoundError('file not found')
    enc = r.pop('data_enc')
    data = base64.b64decode(field_cipher().decrypt(bytes(enc), _aad(str(company_id), str(file_id))))
    return {**_row(r), 'data': data}


def delete_file(company_id: str, file_id: str) -> None:
    """Soft delete: the bytes are wiped, the row stays as a record that a file existed."""
    StoredFile.objects.for_company(company_id).filter(pk=file_id).update(data_enc=None, deleted_at=Now())


def stored_file_response(f: dict, *, download: bool = False) -> HttpResponse:
    """A decrypted file. PDFs and images open in the browser unless `download`; nothing is ever sniffed as HTML."""
    name = f['filename']
    ascii_name = re.sub(r'[^\x20-\x7e]', '_', name).replace('"', "'")
    res = HttpResponse(f['data'], content_type=f['mime'])
    res['X-Content-Type-Options'] = 'nosniff'
    res['Cache-Control'] = 'private, no-store'
    res['Content-Security-Policy'] = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
    res['Content-Disposition'] = (f'{"attachment" if download else "inline"}; filename="{ascii_name}"; '
                                  f"filename*=UTF-8''{quote(name, safe="-_.!~*'()")}")
    return res


def purge_unattached_files(at: datetime | None = None) -> int:
    """Nightly: drop uploads never attached to anything within a day."""
    at = at or _now()
    return StoredFile.objects.filter(attached_to__isnull=True, deleted_at__isnull=True,
                                     created_at__lt=at - timedelta(days=1)).update(data_enc=None, deleted_at=at)
