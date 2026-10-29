"""HTTP port of apps/api/test/files.test.ts over /api/v1/files (the paths the React app's uploadFile uses), with company
isolation checks."""
import base64
import json

from django.test import Client

from hajiri.files.store import attach_file
from tests.core.fixtures import bearer
from tests.multicompany.conftest import world  # noqa: F401  (fixture)
from tests.support.sql import one

PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').decode()


def upload(token, body):
    return Client().post('/api/v1/files', json.dumps(body), content_type='application/json', **bearer(token))


def test_stores_an_encrypted_pdf_and_serves_it_back_to_the_uploader(demo, login_as):
    emp = login_as(['employee'], demo['ram'])
    up = upload(emp.token, {'purpose': 'expense_receipt', 'filename': 'bill.pdf', 'dataUrl': PDF})
    assert up.status_code == 201
    f = up.json()['file']
    assert set(f) == {'id', 'filename', 'mime', 'bytes', 'purpose'}
    assert (f['filename'], f['mime'], f['purpose']) == ('bill.pdf', 'application/pdf', 'expense_receipt')
    assert b'%PDF' not in bytes(one('SELECT data_enc FROM stored_file WHERE id = %s', [f['id']])['data_enc'])
    got = Client().get(f"/api/v1/files/{f['id']}", **bearer(emp.token))
    assert got.status_code == 200
    assert 'application/pdf' in got['Content-Type'] and 'sandbox' in got['Content-Security-Policy']
    assert got.content.startswith(b'%PDF')
    assert Client().get(f"/api/v1/files/{f['id']}?download=1", **bearer(emp.token))['Content-Disposition'].startswith('attachment')


def test_rejects_bad_files_and_bodies(demo, login_as):
    emp = login_as(['employee'], demo['ram'])
    fake = 'data:application/pdf;base64,' + base64.b64encode(b'<html><script>alert(1)</script>').decode()
    assert upload(emp.token, {'purpose': 'expense_receipt', 'filename': 'x.pdf', 'dataUrl': fake}).status_code == 400
    assert upload(emp.token, {'purpose': 'expense_receipt', 'filename': 'x.html', 'dataUrl': 'data:text/html;base64,PGh0bWw+'}).status_code == 400
    bad = upload(emp.token, {'purpose': 'nope', 'filename': 'x.pdf', 'dataUrl': PDF})
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert Client().post('/api/v1/files', '{}', content_type='application/json').status_code == 401
    assert Client().get('/api/v1/files/not-a-uuid', **bearer(emp.token)).status_code == 400


def test_only_the_uploader_or_hr_open_it_and_attached_files_are_served_by_their_module(demo, login_as):
    emp = login_as(['employee'], demo['ram'])
    fid = upload(emp.token, {'purpose': 'expense_receipt', 'filename': 'b.pdf', 'dataUrl': PDF}).json()['file']['id']
    other = Client().get(f'/api/v1/files/{fid}', **bearer(login_as(['manager']).token))
    assert other.status_code == 404 and other.json() == {'error': 'file not found'}
    assert Client().get(f'/api/v1/files/{fid}', **bearer(login_as(['hr']).token)).status_code == 200
    owner = one('SELECT uploaded_by, company_id FROM stored_file WHERE id = %s', [fid])
    attach_file(company_id=str(owner['company_id']), file_id=fid, user_id=str(owner['uploaded_by']),
                purpose='expense_receipt', attached_to='x:1')
    after = Client().get(f'/api/v1/files/{fid}', **bearer(emp.token))
    assert after.status_code == 403 and after.json() == {'error': 'open this file from the page it belongs to'}


def test_another_companys_upload_is_not_found(world):
    beta, _ = world.session(world.admin_b)
    r = upload(beta, {'purpose': 'expense_receipt', 'filename': 'b.pdf', 'dataUrl': PDF})
    assert r.status_code == 201
    fid = r.json()['file']['id']
    assert str(one('SELECT company_id FROM stored_file WHERE id = %s', [fid])['company_id']) == str(world.beta.id)
    for person in (world.admin_a, world.admin_c):
        token, _ = world.session(person)
        assert Client().get(f'/api/v1/files/{fid}', **bearer(token)).json() == {'error': 'file not found'}
    switched, _ = world.session(world.admin_a, world.beta)
    assert Client().get(f'/api/v1/files/{fid}', **bearer(switched)).status_code == 200
