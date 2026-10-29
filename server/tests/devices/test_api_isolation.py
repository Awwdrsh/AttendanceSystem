"""Groups of companies through /api/v1: a Beta device, template, consent or employee is 'not found' for the outsider
(Gamma's admin) and for Alpha's group admin or group HR while they work in Alpha; Beta's own admin, or the group admin
after switching to Beta, reaches it. The Overview JSON counts only the active company."""
import pytest

from hajiri.db.models import BiometricTemplate, ConsentRecord, Device, DeviceCommand
from tests.multicompany.conftest import world  # noqa: F401  (the shared fixture)

from .jsonapi import Api
from .test_isolation import beta_kit  # noqa: F401  (the Beta terminal, template and consent)


def _gets(k):
    e = k['emp']
    return [f'/api/v1/biometrics/consent?employeeId={e}', f'/api/v1/biometrics/templates?employeeId={e}',
            f'/api/v1/biometrics/dsar?employeeId={e}']


def _posts(k):
    d = k['device']
    return [(f'/api/v1/devices/{d}/disable', {}), (f"/api/v1/devices/{k['hik']}/key", {}),
            (f'/api/v1/devices/{d}/commands', {'command': 'INFO'}), (f'/api/v1/devices/{d}/activate', {'name': 'Taken'}),
            (f"/api/v1/biometrics/consent/{k['consent']}/revoke", {}),
            (f"/api/v1/biometrics/templates/{k['template']}/shred", {}),
            (f"/api/v1/biometrics/employees/{k['emp']}/shred-all", {}),
            ('/api/v1/biometrics/consent', {'employeeId': k['emp'], 'modality': 'face', 'noticeVersion': 'v2'})]


def test_beta_records_are_not_found_from_alpha_and_from_outside(world, beta_kit):  # noqa: F811
    for person, company in ((world.admin_c, None), (world.admin_a, world.alpha), (world.ghr, world.alpha)):
        api = Api(world.session(person, company)[0])
        for path in _gets(beta_kit):
            assert api.get(path).status_code == 404, (person.email, path)
        for path, body in _posts(beta_kit):
            assert api.post(path, body).status_code == 404, (person.email, path)
        # Lists answer empty rather than 404, as the TS queries did.
        assert api.get(f"/api/v1/devices/{beta_kit['device']}/mappings").json() == {'mappings': []}
        assert api.get(f"/api/v1/devices/{beta_kit['device']}/commands").json() == {'commands': []}
        assert api.post(f"/api/v1/devices/{beta_kit['device']}/sync-users").json() == {'queued': 0}
        assert beta_kit['device'] not in {d['id'] for d in api.get('/api/v1/devices').json()['devices']}
        assert '909' not in {p['device_user_pin'] for p in api.get('/api/v1/devices/punches/unmapped').json()['punches']}
    assert Device.objects.get(pk=beta_kit['device']).status == 'active'
    assert BiometricTemplate.objects.get(pk=beta_kit['template']).shredded_at is None
    assert ConsentRecord.objects.get(pk=beta_kit['consent']).revoked_at is None
    assert DeviceCommand.objects.filter(device_id=beta_kit['device']).count() == 1


@pytest.mark.parametrize('who', ['admin_b', 'admin_a'])
def test_beta_admin_and_the_group_admin_after_switching_reach_the_beta_records(world, beta_kit, who):  # noqa: F811
    api = Api(world.session(getattr(world, who), world.beta)[0])
    for path in _gets(beta_kit):
        assert api.get(path).status_code == 200, path
    assert len(api.get(f"/api/v1/devices/{beta_kit['device']}/mappings").json()['mappings']) == 1
    assert len(api.get(f"/api/v1/devices/{beta_kit['device']}/commands").json()['commands']) == 1
    assert beta_kit['device'] in {d['id'] for d in api.get('/api/v1/devices').json()['devices']}
    assert ['909'] == [p['device_user_pin'] for p in api.get('/api/v1/devices/punches/unmapped').json()['punches']]
    assert api.get('/api/v1/biometrics/stats').json()['byModality'] == [
        {'modality': 'face', 'active': 1, 'shredded': 0, 'unmapped': 0}]


def test_the_executive_dashboard_counts_only_the_active_company(world, beta_kit):  # noqa: F811
    def tiles(company):
        body = Api(world.session(world.gexec, company)[0]).get('/api/v1/dashboard/executive').json()
        return {t['key']: t['value'] for t in body['tiles']}
    assert tiles(world.alpha)['headcount'] == 4
    assert tiles(world.beta)['headcount'] == 4
