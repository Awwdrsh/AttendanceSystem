"""Uploads in a group of companies (tests/multicompany world): an upload belongs to the company the person was working
in, and another company's upload is not found, even for someone who may switch to that company."""
import base64
import json

import pytest
from django.test import Client

from hajiri.core.errors import NotFoundError
from hajiri.db.models import StoredFile
from hajiri.files import service
from tests.multicompany.conftest import world  # noqa: F401  (fixture)
from tests.web.factories import bearer

pytestmark = pytest.mark.django_db

PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n%%EOF').decode()


def _upload(user):
    return service.upload(user, {'purpose': 'expense_receipt', 'filename': 'bill.pdf', 'dataUrl': PDF})


def test_an_upload_is_stored_in_the_active_company(world):
    _, dual_in_beta = world.session(world.dual, world.beta)
    f = _upload(dual_in_beta)
    assert str(StoredFile.objects.get(pk=f['id']).company_id) == str(world.beta.id)
    _, dual_at_home = world.session(world.dual)
    with pytest.raises(NotFoundError):
        service.open_unattached(dual_at_home, f['id'])
    assert service.open_unattached(dual_in_beta, f['id'])['data'].startswith(b'%PDF')


def test_another_companys_upload_is_not_found_over_http(world):
    _, beta_admin = world.session(world.admin_b)
    fid = _upload(beta_admin)['id']
    alpha_token, _ = world.session(world.admin_a)                    # group admin, working in Alpha
    outsider, _ = world.session(world.admin_c)
    for token in (alpha_token, outsider):
        r = Client().get(f'/api/v1/files/{fid}', **bearer(token))
        assert r.status_code == 404 and r.json() == {'error': 'file not found'}
    beta_token, _ = world.session(world.admin_b)
    assert Client().get(f'/api/v1/files/{fid}', **bearer(beta_token)).status_code == 200
    switched, _ = world.session(world.admin_a, world.beta)           # admin of Beta through the group, after a switch
    assert Client().get(f'/api/v1/files/{fid}', **bearer(switched)).status_code == 200


def test_upload_endpoint_files_into_the_active_company(world):
    token, _ = world.session(world.dual, world.beta)
    r = Client().post('/api/v1/files', json.dumps({'purpose': 'expense_receipt', 'filename': 'b.pdf', 'dataUrl': PDF}),
                      content_type='application/json', **bearer(token))
    assert r.status_code == 201
    assert str(StoredFile.objects.get(pk=r.json()['file']['id']).company_id) == str(world.beta.id)

