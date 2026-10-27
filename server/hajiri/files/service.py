"""Uploads by signed-in users (port of apps/api/src/modules/files/routes.ts). Each module attaches an upload to its own
record with store.attach_file and from then on serves it with its own access rules."""
from __future__ import annotations

from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.db import one
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError, TooManyRequestsError
from hajiri.core.schema import Enum, Obj, Str, parse

from .store import DOCUMENT_MIMES, IMAGE_MIMES, parse_upload, parse_uploaded_file, read_file, save_file, file_meta

UPLOAD_PURPOSES = ('employee_document', 'company_document', 'expense_receipt', 'travel_bill', 'helpdesk_attachment',
                   'training_certificate', 'profile_change', 'hiring_cv', 'avatar', 'tax_proof', 'grievance_attachment',
                   'task_attachment')
UPLOADS_PER_HOUR = 300
UNATTACHED_BYTES_MAX = 40 * 1024 * 1024

UPLOAD_BODY = Obj({'purpose': Enum(UPLOAD_PURPOSES), 'filename': Str(min=1, max=200), 'dataUrl': Str(min=10)})


def _check_quota(user: AuthUser) -> None:
    # Unattached uploads are only purged after a day, so cap what one person can park in the meantime.
    recent = one("""SELECT count(*)::int AS n FROM stored_file WHERE uploaded_by = %s AND created_at > now() - interval '1 hour'""",
                 [user.id])
    parked = one("""SELECT coalesce(sum(bytes), 0)::bigint AS bytes FROM stored_file
                     WHERE uploaded_by = %s AND attached_to IS NULL AND deleted_at IS NULL""", [user.id])
    if recent['n'] >= UPLOADS_PER_HOUR or int(parked['bytes']) >= UNATTACHED_BYTES_MAX:
        raise TooManyRequestsError('Too many uploads. Finish the forms you have open or try again later.')


def _public(row: dict) -> dict:
    return {'id': row['id'], 'filename': row['filename'], 'mime': row['mime'], 'bytes': row['bytes'], 'purpose': row['purpose']}


def upload(user: AuthUser, body: dict) -> dict:
    """JSON upload ({purpose, filename, dataUrl}); returns {id, filename, mime, bytes, purpose}."""
    b = parse(UPLOAD_BODY, body)
    _check_quota(user)
    allowed = IMAGE_MIMES if b['purpose'] == 'avatar' else DOCUMENT_MIMES
    f = parse_upload(b['dataUrl'], allowed=allowed)
    row = save_file(company_id=company_id_of(user), purpose=b['purpose'], filename=b['filename'], file=f, uploaded_by=user.id)
    return _public(row)


def upload_file(user: AuthUser, purpose: str, uploaded, filename: str | None = None) -> dict:
    """Multipart upload from a form (request.FILES['file'])."""
    if purpose not in UPLOAD_PURPOSES:
        raise BadRequestError('unknown upload purpose')
    if uploaded is None:
        raise BadRequestError('Choose a file to upload.')
    name = (filename or getattr(uploaded, 'name', '') or 'file')[:200]
    _check_quota(user)
    f = parse_uploaded_file(uploaded, allowed=IMAGE_MIMES if purpose == 'avatar' else DOCUMENT_MIMES)
    row = save_file(company_id=company_id_of(user), purpose=purpose, filename=name, file=f, uploaded_by=user.id)
    return _public(row)


def open_unattached(user: AuthUser, file_id: str) -> dict:
    """An upload not yet attached, for its uploader (or HR); attached files are served by their module."""
    company_id = company_id_of(user)
    meta = file_meta(company_id, file_id)
    if not meta:
        raise NotFoundError('file not found')
    if meta['attached_to']:
        raise ForbiddenError('open this file from the page it belongs to')
    if meta['uploaded_by'] != user.id and not has_role(user, 'hr'):
        raise NotFoundError('file not found')
    return read_file(company_id, file_id)
