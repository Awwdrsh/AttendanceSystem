"""Devices belong to a company: in a group of companies, one company's terminal never records attendance, templates
or commands for a sister company, and the reconcile command can be limited to one company."""
import base64
import json
from datetime import date

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.backends.postgresql.psycopg_any import DateRange

from hajiri.db.models import (
    BiometricTemplate, Company, CompanyGroup, ConsentRecord, Device, DeviceCommand, DeviceUserMap, Employee, PunchEvent,
    RecomputeQueue,
)
from hajiri.gateway.punch import Signer, TemplateCipher
from hajiri.gateway.store import PostgresStore

from .conftest import npt_now

pytestmark = pytest.mark.django_db
KEY = bytes([7]) * 32
FOREVER = DateRange(date(2020, 1, 1), None)


@pytest.fixture
def group(make_app):
    g = CompanyGroup.objects.create(code='GRP', name_en='Group')
    alpha = Company.objects.create(code='ALPHA', name_en='Alpha', group=g)
    beta = Company.objects.create(code='BETA', name_en='Beta', group=g)
    ea = Employee.objects.create(company=alpha, emp_code='A1', name_en='Asha')
    eb = Employee.objects.create(company=beta, emp_code='B1', name_en='Bina')
    da = Device.objects.create(company=alpha, serial_no='ALPHA-01', vendor='zkteco', protocol='adms', status='active')
    db_ = Device.objects.create(company=beta, serial_no='BETA-01', vendor='zkteco', protocol='adms', status='active')
    for e in (ea, eb):
        ConsentRecord.objects.create(employee=e, purpose='attendance', modality='fingerprint', notice_version='v1')
    store = PostgresStore()
    app = make_app(store, signer=Signer.ephemeral_key(), cipher=TemplateCipher(KEY))
    return {'alpha': alpha, 'beta': beta, 'ea': ea, 'eb': eb, 'da': da, 'db': db_, 'store': store, 'app': app}


def _punch(app, sn, pin='1'):
    return app.post(f'/iclock/cdata?SN={sn}&table=ATTLOG', f'{pin}\t{npt_now()}\t0\t1\t0\n')


def test_device_record_carries_its_company(group):
    rec = group['store'].get_device('ALPHA-01')
    assert rec.company_id == str(group['alpha'].id)
    assert [d.serial_no for d in group['store'].list_devices(company_id=str(group['beta'].id))] == ['BETA-01']
    assert [d.serial_no for d in group['store'].list_devices()] == ['ALPHA-01', 'BETA-01']


def test_the_same_pin_on_two_companies_terminals_maps_to_each_companys_own_employee(group):
    DeviceUserMap.objects.create(device=group['da'], device_user_pin='1', employee=group['ea'], valid=FOREVER)
    DeviceUserMap.objects.create(device=group['db'], device_user_pin='1', employee=group['eb'], valid=FOREVER)
    assert _punch(group['app'], 'ALPHA-01').text == 'OK: 1'
    assert _punch(group['app'], 'BETA-01').text == 'OK: 1'
    got = dict(PunchEvent.objects.values_list('device_sn', 'employee_id'))
    assert got == {'ALPHA-01': group['ea'].id, 'BETA-01': group['eb'].id}
    assert PunchEvent.objects.for_company(group['alpha'].id).count() == 1
    assert set(RecomputeQueue.objects.for_company(group['beta'].id).values_list('employee_id', flat=True)) == {group['eb'].id}


def test_a_mapping_to_a_sister_companys_employee_is_ignored(group):
    # Alpha's terminal with PIN 1 pointing at Beta's employee (a stale or forged mapping): stored unmapped, no recompute.
    DeviceUserMap.objects.create(device=group['da'], device_user_pin='1', employee=group['eb'], valid=FOREVER)
    assert _punch(group['app'], 'ALPHA-01').text == 'OK: 1'
    p = PunchEvent.objects.get()
    assert p.employee_id is None
    assert not RecomputeQueue.objects.exists()
    assert PunchEvent.objects.for_company(group['beta'].id).count() == 0


def test_templates_are_never_stored_against_a_sister_companys_employee(group):
    DeviceUserMap.objects.create(device=group['da'], device_user_pin='1', employee=group['eb'], valid=FOREVER)
    DeviceUserMap.objects.create(device=group['da'], device_user_pin='2', employee=group['ea'], valid=FOREVER)
    b64 = base64.b64encode(b'tmpl').decode()
    body = f'PIN=1\tFID=6\tSize=1\tValid=1\tTMP={b64}\nPIN=2\tFID=6\tSize=1\tValid=1\tTMP={b64}\n'
    group['app'].post('/iclock/fdata?SN=ALPHA-01&table=FINGERTMP', body, content_type='application/octet-stream')
    assert list(BiometricTemplate.objects.values_list('employee_id', flat=True)) == [group['ea'].id]
    assert BiometricTemplate.objects.for_company(group['beta'].id).count() == 0


def test_a_terminal_cannot_acknowledge_or_receive_a_sister_companys_commands(group):
    store, app = group['store'], group['app']
    cid = store.enqueue_command(str(group['db'].id), 'INFO')
    assert app.get('/iclock/getrequest?SN=ALPHA-01').text == 'OK'
    assert DeviceCommand.objects.get(id=cid).status == DeviceCommand.Status.QUEUED
    assert app.get('/iclock/getrequest?SN=BETA-01').text == f'C:{cid}:INFO\n'
    app.post('/iclock/devicecmd?SN=ALPHA-01', f'ID={cid}&Return=0&CMD=INFO')
    assert DeviceCommand.objects.get(id=cid).status == DeviceCommand.Status.SENT
    app.post('/iclock/devicecmd?SN=BETA-01', f'ID={cid}&Return=0&CMD=INFO')
    assert DeviceCommand.objects.get(id=cid).status == DeviceCommand.Status.ACKED


def test_a_pending_terminal_belongs_to_no_company_and_maps_nobody(group):
    assert group['app'].get('/iclock/cdata?SN=NEW-01&options=all').text == 'OK'
    d = Device.objects.get(serial_no='NEW-01')
    assert d.company_id is None and d.status == Device.Status.PENDING
    assert Device.objects.for_company(group['alpha'].id).filter(serial_no='NEW-01').count() == 0


def test_reconcile_command_can_be_limited_to_one_company(group, capsys):
    call_command('gateway_reconcile', '--all', '--company', 'beta', '--date', '2026-09-21')
    lines = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
    assert [x['sn'] for x in lines] == ['BETA-01']
    call_command('gateway_reconcile', '--all', '--company', str(group['alpha'].id), '--date', '2026-09-21')
    assert [json.loads(x)['sn'] for x in capsys.readouterr().out.strip().splitlines()] == ['ALPHA-01']
    assert DeviceCommand.objects.for_company(group['alpha'].id).count() == 1
    with pytest.raises(CommandError, match='company not found'):
        call_command('gateway_reconcile', '--all', '--company', 'NOPE')


def test_recompute_upsert_refreshes_the_reason(group):
    store = group['store']
    store.enqueue_recompute(str(group['ea'].id), '2026-09-21', 'punch:zkteco')
    store.enqueue_recompute(str(group['ea'].id), '2026-09-21', 'punch:hikvision')
    assert list(RecomputeQueue.objects.values_list('reason', flat=True)) == ['punch:hikvision']


def test_the_same_record_twice_is_a_duplicate_not_an_error(group):
    DeviceUserMap.objects.create(device=group['da'], device_user_pin='1', employee=group['ea'], valid=FOREVER)
    line = f'1\t{npt_now()}\t0\t1\t0\n'
    group['app'].post('/iclock/cdata?SN=ALPHA-01&table=ATTLOG', line)
    assert group['app'].post('/iclock/cdata?SN=ALPHA-01&table=ATTLOG', line).text == 'OK: 1'
    assert PunchEvent.objects.count() == 1


def test_ingest_costs_a_fixed_number_of_queries_per_line(group):
    """Per punch: savepoint + insert, near-duplicate check, PIN lookup and one or two recompute upserts (a punch
    near midnight also touches the previous day), so at most 8 statements per line and nothing per request that grows."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from .conftest import npt_now as t
    DeviceUserMap.objects.create(device=group['da'], device_user_pin='1', employee=group['ea'], valid=FOREVER)

    def cost(n, offset):
        body = ''.join(f'1\t{t(offset - i * 60_000)}\t0\t1\t0\n' for i in range(n))
        with CaptureQueriesContext(connection) as ctx:
            assert group['app'].post('/iclock/cdata?SN=ALPHA-01&table=ATTLOG', body).text == f'OK: {n}'
        return len(ctx.captured_queries)

    one_line, three_lines, five_lines = cost(1, -3_600_000), cost(3, -7_200_000), cost(5, -10_800_000)
    # Measured against the one-line batch: the batches lie at different times of day, and whether a punch also
    # touches the previous day depends on that, so two batches' per-line costs may differ by one statement.
    assert three_lines - one_line <= 2 * 8
    assert five_lines - one_line <= 4 * 8
    assert one_line <= 12
