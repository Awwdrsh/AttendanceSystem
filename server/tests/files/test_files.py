"""Port of apps/api/test/files.test.ts, through the upload endpoint and the store."""
import base64
import json
from datetime import timedelta

import pytest
from django.test import Client

from hajiri.core.clock import now as _now
from tests.support.sql import execute, one
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError, TooManyRequestsError
from hajiri.files import service
from hajiri.files.store import attach_file, clean_filename, parse_upload, purge_unattached_files, read_file
from tests.core.fixtures import bearer

PDF_BYTES = b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF'
PDF = 'data:application/pdf;base64,' + base64.b64encode(PDF_BYTES).decode()
PNG = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'\x00' * 16).decode()


def post(token, body):
    return Client().post('/api/v1/files', json.dumps(body), content_type='application/json', **bearer(token))


def test_rejects_a_file_whose_bytes_do_not_match_the_type(demo, login_as):
    emp = login_as(['employee'], demo['ram'])
    bad = 'data:application/pdf;base64,' + base64.b64encode(b'<html><script>alert(1)</script>').decode()
    r = post(emp.token, {'purpose': 'expense_receipt', 'filename': 'x.pdf', 'dataUrl': bad})
    assert r.status_code == 400 and r.json()['error'] == 'The file could not be read. Check that it is not damaged.'
    html = post(emp.token, {'purpose': 'expense_receipt', 'filename': 'x.html', 'dataUrl': 'data:text/html;base64,PGh0bWw+'})
    assert html.status_code == 400
    # An avatar must be an image.
    assert post(emp.token, {'purpose': 'avatar', 'filename': 'me.pdf', 'dataUrl': PDF}).status_code == 400
    assert post(emp.token, {'purpose': 'avatar', 'filename': 'me.png', 'dataUrl': PNG}).status_code == 201
    assert post(emp.token, {'purpose': 'nope', 'filename': 'x.pdf', 'dataUrl': PDF}).json()['error'] == 'validation'


def test_purges_uploads_never_attached_within_a_day(demo, login_as):
    emp = login_as(['employee'], demo['ram'])
    post(emp.token, {'purpose': 'expense_receipt', 'filename': 'b.pdf', 'dataUrl': PDF})
    assert purge_unattached_files(_now() + timedelta(days=2)) > 0
    assert one('SELECT count(*)::int AS n FROM stored_file WHERE attached_to IS NULL AND data_enc IS NOT NULL')['n'] == 0


def test_upload_quota(demo, login_as):
    emp = login_as(['employee'], demo['ram'])
    execute("""INSERT INTO stored_file (company_id, purpose, filename, mime, bytes, sha256, uploaded_by)
               SELECT %s, 'expense_receipt', 'f', 'application/pdf', 1, 'x', %s FROM generate_series(1, 300)""",
            [demo['company_id'], emp.user_id])
    with pytest.raises(TooManyRequestsError):
        service.upload(emp.user, {'purpose': 'expense_receipt', 'filename': 'b.pdf', 'dataUrl': PDF})
    assert post(emp.token, {'purpose': 'expense_receipt', 'filename': 'b.pdf', 'dataUrl': PDF}).status_code == 429


def test_parse_upload_limits_and_filenames():
    big = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-' + b'0' * (4 * 1024 * 1024)).decode()
    with pytest.raises(BadRequestError, match=r'too large \(max 4 MB\)'):
        parse_upload(big)
    with pytest.raises(BadRequestError, match='must be a PDF, JPEG, PNG, WEBP'):
        parse_upload('data:image/gif;base64,R0lGODlh')
    assert clean_filename('  a/b\\c<d>.pdf \n') == 'a_b_c_d_.pdf _'  # control characters become _ before trimming
    assert clean_filename(' x\ty .pdf ') == 'x_y .pdf'
    assert clean_filename('') == 'file'


def test_another_companys_file_is_not_found(demo, login_as):
    emp = login_as(['employee'], demo['ram'])
    fid = post(emp.token, {'purpose': 'expense_receipt', 'filename': 'b.pdf', 'dataUrl': PDF}).json()['file']['id']
    other = one("INSERT INTO company (code, name_en) VALUES ('O2', 'Other') RETURNING id")['id']
    ohr = login_as(['hr'], company_id=str(other))
    with pytest.raises(NotFoundError):
        service.open_unattached(ohr.user, fid)
