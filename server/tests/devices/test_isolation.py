"""Groups of companies: every devices, biometrics and Overview page and service sees only the ACTIVE company. A Beta
record is 'not found' for the outsider (Gamma's admin) and for Alpha's group admin while they work in Alpha; Beta's
own admin, or the group admin after switching to Beta, gets it."""
from datetime import date, datetime, timezone

import pytest
from django.db.backends.postgresql.psycopg_any import DateRange

from hajiri.core.errors import NotFoundError
from hajiri.dashboard import services as dash
from hajiri.db.models import BiometricTemplate, ConsentRecord, Device, DeviceCommand, DeviceUserMap, PunchEvent
from hajiri.devices import biometrics as bio
from hajiri.devices import services as dev
from tests.multicompany.conftest import world  # noqa: F401  (the shared fixture)
from tests.web.factories import htmx, make_employee


@pytest.fixture
def beta_kit(world):  # noqa: F811
    """A Beta terminal with one mapped PIN, a queued command, an unmapped punch, a template and a consent."""
    emp = make_employee(world.beta, name='Bina Beta')
    d = Device.objects.create(company=world.beta, serial_no='BETA-ZK-1', vendor='zkteco', protocol='adms', status='active',
                              name='Beta gate')
    hik = Device.objects.create(company=world.beta, serial_no='BETA-HIK-1', vendor='hikvision', protocol='isapi',
                                status='active')
    DeviceUserMap.objects.create(device=d, device_user_pin='5', employee=emp, valid=DateRange(date(2026, 1, 1), None))
    DeviceCommand.objects.create(device=d, command='CHECK')
    PunchEvent.objects.create(device=d, device_sn='BETA-ZK-1', vendor='zkteco', device_user_pin='909',
                              punched_at=datetime(2026, 9, 28, 4, 15, tzinfo=timezone.utc), device_local_time='2026-09-28 10:00:00',
                              local_date=date(2026, 9, 28), direction='in', verify_mode='fingerprint', source='zkteco',
                              idempotency_key='beta-unmapped-1')
    consent = ConsentRecord.objects.create(employee=emp, purpose='attendance', modality='face', notice_version='v1')
    tpl = BiometricTemplate.objects.create(employee=emp, device_user_pin='5', modality='face', vendor_format='zk_fp_v10',
                                           ciphertext=b'\x01', aad_hash='h', enrolled_on_device=d, consent=consent)
    return {'emp': str(emp.id), 'device': str(d.id), 'hik': str(hik.id), 'consent': str(consent.id), 'template': str(tpl.id)}


def _gets(k):
    e = k['emp']
    return [f"/_/devices/{k['device']}", f"/_/devices/{k['device']}/commands", f"/_/devices/{k['device']}/mappings",
            f"/_/devices/{k['device']}/activate", f"/_/devices/map?device={k['device']}&pin=5",
            f'/_/devices/biometrics/consents?employeeId={e}', f'/_/devices/biometrics/templates?employeeId={e}',
            f'/_/devices/biometrics/dsar?employeeId={e}']


def _posts(k):
    return [(f"/_/devices/{k['device']}/disable", {}), (f"/_/devices/{k['hik']}/key", {}),
            (f"/_/devices/{k['device']}/sync-users", {}), (f"/_/devices/{k['device']}/command", {'command': 'INFO'}),
            (f"/_/devices/{k['device']}/activate", {'name': 'Taken'}),
            (f"/_/devices/biometrics/consent/{k['consent']}/revoke", {}),
            (f"/_/devices/biometrics/templates/{k['template']}/shred", {}),
            (f"/_/devices/biometrics/employees/{k['emp']}/shred-all", {})]


def test_beta_records_are_not_found_from_alpha_and_from_outside(world, beta_kit):  # noqa: F811
    for person, company in ((world.admin_c, None), (world.admin_a, world.alpha), (world.ghr, world.alpha)):
        c = world.signed_in(person, company)
        for path in _gets(beta_kit):
            assert c.get(path, **htmx()).status_code == 404, (person.email, path)
        for path, data in _posts(beta_kit):
            assert c.post(path, data, **htmx()).status_code == 404, (person.email, path)
        # A Beta employee cannot be given a PIN on an Alpha terminal either.
        alpha_dev = Device.objects.create(company_id=world.alpha.id if company else world.gamma.id,
                                          serial_no=f'X-{person.email}', vendor='zkteco', protocol='adms', status='active')
        r = c.post('/_/devices/map', {'device_id': str(alpha_dev.id), 'pin': '5', 'employee_id': beta_kit['emp'],
                                      'valid_from': '2026-09-01'}, **htmx())
        assert r.status_code == 404, person.email
    # Nothing was changed.
    assert Device.objects.get(pk=beta_kit['device']).status == 'active'
    assert BiometricTemplate.objects.get(pk=beta_kit['template']).shredded_at is None
    assert ConsentRecord.objects.get(pk=beta_kit['consent']).revoked_at is None
    assert DeviceCommand.objects.filter(device_id=beta_kit['device']).count() == 1


def test_beta_admin_and_the_group_admin_after_switching_see_the_beta_records(world, beta_kit):  # noqa: F811
    for person in (world.admin_b, world.admin_a):
        c = world.signed_in(person, world.beta)
        for path in _gets(beta_kit):
            assert c.get(path, **htmx()).status_code == 200, (person.email, path)


def test_lists_show_only_the_active_companys_terminals_and_punches(world, beta_kit):  # noqa: F811
    Device.objects.create(company=world.alpha, serial_no='ALPHA-ZK-1', vendor='zkteco', protocol='adms', status='active')
    in_alpha = world.signed_in(world.admin_a, world.alpha).get('/settings/devices').content.decode()
    assert 'ALPHA-ZK-1' in in_alpha and 'BETA-ZK-1' not in in_alpha and '909' not in in_alpha
    in_beta = world.signed_in(world.admin_a, world.beta).get('/settings/devices').content.decode()
    assert 'BETA-ZK-1' in in_beta and 'ALPHA-ZK-1' not in in_beta and '909' in in_beta

    _, alpha_user = world.session(world.admin_a, world.alpha)
    _, beta_user = world.session(world.admin_a, world.beta)
    assert {d['serial_no'] for d in dev.list_devices(alpha_user)} == {'ALPHA-ZK-1'}
    assert dev.unmapped_punches(alpha_user) == [] and [p['device_user_pin'] for p in dev.unmapped_punches(beta_user)] == ['909']
    assert dev.mappings(alpha_user, beta_kit['device']) == [] and len(dev.mappings(beta_user, beta_kit['device'])) == 1
    assert bio.stats(alpha_user) == [] and bio.stats(beta_user) == [{'modality': 'face', 'active': 1, 'shredded': 0, 'unmapped': 0}]
    with pytest.raises(NotFoundError):
        bio.templates(alpha_user, {'employeeId': beta_kit['emp']})
    assert len(bio.templates(beta_user, {'employeeId': beta_kit['emp']})) == 1


def test_an_unclaimed_terminal_is_offered_to_each_company_until_one_activates_it(world):  # noqa: F811
    pending = Device.objects.create(serial_no='NEW-ZK-9', vendor='zkteco', protocol='adms')
    _, alpha_user = world.session(world.admin_a, world.alpha)
    _, gamma_user = world.session(world.admin_c)
    assert 'NEW-ZK-9' in {d['serial_no'] for d in dev.list_devices(gamma_user)}
    dev.activate(alpha_user, str(pending.id), {'name': 'Alpha back door'})
    assert 'NEW-ZK-9' not in {d['serial_no'] for d in dev.list_devices(gamma_user)}
    with pytest.raises(NotFoundError):
        dev.activate(gamma_user, str(pending.id), {})
    assert str(Device.objects.get(pk=pending.id).company_id) == str(world.alpha.id)


def test_the_overview_counts_only_the_active_company(world, beta_kit):  # noqa: F811
    make_employee(world.beta, name='Second Beta')
    _, exec_alpha = world.session(world.gexec, world.alpha)
    _, exec_beta = world.session(world.gexec, world.beta)
    tiles = lambda user: {t['key']: t['value'] for t in dash.executive(user)['tiles']}  # noqa: E731
    assert tiles(exec_alpha)['headcount'] == 4          # Asha, Sita, Dipak, Kiran
    assert tiles(exec_beta)['headcount'] == 5           # Bimal, Dipak at Beta, Gopal, Bina, Second Beta
    alpha_attention = {n['kind'] for n in dash.executive(exec_alpha)['needsAttention']}
    beta_attention = {n['kind'] for n in dash.executive(exec_beta)['needsAttention']}
    assert 'unmapped_punches' not in alpha_attention and 'device_offline' not in alpha_attention
    assert {'unmapped_punches', 'device_offline'} <= beta_attention
    body = world.signed_in(world.gexec, world.beta).get('/overview').content.decode()
    assert 'Beta gate is offline' in body
    assert 'Beta gate is offline' not in world.signed_in(world.gexec, world.alpha).get('/overview').content.decode()
