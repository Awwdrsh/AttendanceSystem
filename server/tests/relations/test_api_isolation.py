"""Groups of companies over the /api/v1/relations JSON API: a sister company's records are not found (404) for the
group admin working in Alpha and for an outside company's admin, until the group admin switches to Beta."""
import pytest

from hajiri.db.models import DisciplinaryCase, Grievance, KyeCampaign, RegisteredDevice

from .test_isolation import NOWHERE, client_in, grp  # noqa: F401  (grp is the fixture)

R = '/api/v1/relations'


def _gets(w):
    return [f'{R}/grievances/{w.grievance}', f'{R}/grievances/{w.grievance}/files/{w.file}', f'{R}/disciplinary/{w.case}',
            f'{R}/kye/campaigns/{w.kye}', f'{R}/employees/{w.beta_e.id}/contract']


def _writes(w):
    return [('post', f'{R}/grievances/{w.grievance}/notes', {'body': 'x'}),
            ('patch', f'{R}/grievances/{w.grievance}', {'status': 'under_review'}),
            ('post', f'{R}/grievances/{w.grievance}/decision', {'outcome': 'upheld', 'findings': 'x'}),
            ('post', f'{R}/grievances/{w.grievance}/close', {}),
            ('post', f'{R}/disciplinary/{w.case}/show-cause', {'text': 'Explain', 'responseDue': '2099-01-01'}),
            ('post', f'{R}/disciplinary/{w.case}/decide', {'stageId': None, 'note': 'x', 'force': True}),
            ('post', f'{R}/disciplinary/{w.case}/withdraw', {'reason': 'x'}),
            ('post', f'{R}/kye/campaigns/{w.kye}/nudge', {}), ('post', f'{R}/kye/campaigns/{w.kye}/close', {}),
            ('post', f'{R}/assignments/{w.assignment}/end', {'cancel': True}),
            ('put', f'{R}/employees/{w.beta_e.id}/contract', {'contractEnd': '2099-01-01'}),
            ('post', f'{R}/popups/{w.notice}/dismiss', {})]


@pytest.mark.parametrize('who', ['admin_a', 'admin_c'])
def test_a_sister_or_outside_company_record_is_not_found(grp, who):  # noqa: F811
    w = grp
    c = client_in(getattr(w, who))
    for url in _gets(w):
        assert c.get(url).status_code == 404, url
    for method, url, body in _writes(w):
        assert getattr(c, method)(url, body, content_type='application/json').status_code == 404, url
    # A device of another company answers exactly like a device that does not exist (the TS 409).
    foreign = c.post(f'{R}/devices/{w.device}/approve')
    missing = c.post(f'{R}/devices/{NOWHERE}/approve')
    assert foreign.status_code == missing.status_code == 409 and foreign.json() == missing.json()
    # Lists show only the active company.
    assert c.get(f'{R}/disciplinary').json() == {'cases': []}
    assert c.get(f'{R}/kye/campaigns').json() == {'campaigns': []}
    assert c.get(f'{R}/devices').json() == {'devices': []}
    assert c.get(f'{R}/grievances', {'scope': 'handle'}).json() == {'grievances': []}
    assert c.get(f'{R}/summary').json() == {'openGrievances': 0, 'openCases': 0, 'pendingDevices': 0, 'openKye': 0, 'isHr': True}
    # Nothing changed in Beta.
    assert Grievance.objects.get(pk=w.grievance).status == Grievance.Status.SUBMITTED
    assert DisciplinaryCase.objects.get(pk=w.case).status == DisciplinaryCase.Status.OPEN
    assert KyeCampaign.objects.get(pk=w.kye).status == KyeCampaign.Status.OPEN
    assert RegisteredDevice.objects.get(pk=w.device).status == RegisteredDevice.Status.PENDING


def test_the_owner_company_and_the_group_admin_after_switching_can_open_them(grp):  # noqa: F811
    w = grp
    for c in (client_in(w.hr_b), client_in(w.admin_a, w.beta)):
        for url in _gets(w):
            assert c.get(url).status_code == 200, url
    beta = client_in(w.admin_a, w.beta)
    assert [d['id'] for d in beta.get(f'{R}/devices').json()['devices']] == [w.device]
    assert beta.post(f'{R}/devices/{w.device}/approve').json() == {'ok': True}
    assert beta.get(f'{R}/summary').json()['openCases'] == 1
