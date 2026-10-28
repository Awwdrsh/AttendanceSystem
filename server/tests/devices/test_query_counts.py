"""The Devices page, the device drawer and the Overview page run a fixed number of queries however many terminals,
mappings, commands and unmapped punches there are (no N+1)."""
from datetime import date, datetime, timedelta, timezone

import pytest
from django.db import connection
from django.db.backends.postgresql.psycopg_any import DateRange
from django.test import Client
from django.test.utils import CaptureQueriesContext

from hajiri.db.models import Device, DeviceCommand, DeviceUserMap, Employee, PunchEvent
from tests.web.factories import htmx, sign_in


@pytest.fixture
def hr(login_as):
    return login_as(['hr'])


def _add_terminals(company_id, emp_id, n, tag):
    for i in range(n):
        d = Device.objects.create(company_id=company_id, serial_no=f'{tag}-{i}', vendor='zkteco', protocol='adms',
                                  status='active', name=f'Gate {tag}{i}')
        DeviceUserMap.objects.create(device=d, device_user_pin=f'{tag}{i}', employee_id=emp_id,
                                     valid=DateRange(date(2026, 1, 1), None))
        DeviceCommand.objects.create(device=d, command='CHECK')
        DeviceCommand.objects.create(device=d, command='INFO')
        for j in range(2):
            PunchEvent.objects.create(device=d, device_sn=d.serial_no, vendor='zkteco', device_user_pin=f'u{tag}{i}',
                                      punched_at=datetime(2026, 9, 28, 4, j, tzinfo=timezone.utc) + timedelta(days=i),
                                      device_local_time='2026-09-28 10:00:00', local_date=date(2026, 9, 28),
                                      direction='in', verify_mode='fingerprint', source='zkteco',
                                      idempotency_key=f'qc-{tag}-{i}-{j}')


def _count(client, path):
    with CaptureQueriesContext(connection) as ctx:
        r = client.get(path, **htmx())
    assert r.status_code == 200, path
    return len(ctx.captured_queries)


def test_list_pages_do_not_grow_queries_with_rows(demo, hr):
    c = Client()
    sign_in(c, hr.email)
    emp = Employee.objects.get(pk=demo['sita'])
    _add_terminals(emp.company_id, emp.id, 1, 'A')
    dev_id = str(Device.objects.get(serial_no='A-0').id)
    paths = ['/settings/devices', '/settings/devices?tab=unmapped', f'/_/devices/{dev_id}',
             f'/_/devices/{dev_id}/commands', f'/_/devices/{dev_id}/mappings', '/overview']
    for p in paths:      # the first request of a session loads per-session things once
        _count(c, p)
    before = {p: _count(c, p) for p in paths}
    _add_terminals(emp.company_id, emp.id, 6, 'B')
    for i in range(5):
        DeviceUserMap.objects.create(device_id=dev_id, device_user_pin=f'X{i}', employee_id=emp.id,
                                     valid=DateRange(date(2026, 1, 1), None))
        DeviceCommand.objects.create(device_id=dev_id, command='LOG')
    Device.objects.filter(company_id=emp.company_id).update(last_seen_at=None)   # every terminal on the attention list
    after = {p: _count(c, p) for p in paths}
    assert after == before
