"""Development seed (port of apps/api/src/db/seed-demo.ts): company DEMO, one branch, a general 10-18 shift, two
employees and one ZKTeco device mapped to them. The integration tests build on it, with the same fixed ids. Running it
again adds only what is missing."""
from __future__ import annotations

from datetime import date, time

from django.db import transaction
from django.db.backends.postgresql.psycopg_any import DateRange

from hajiri.core.bs_calendar_seed import seed_bs_calendar
from hajiri.db.models import (
    Branch, Company, Device, DeviceUserMap, Employee, EmployeeAssignment, Shift, ShiftAssignment,
)

from .seed import seed_defaults

DEMO = {
    'company_id': '00000000-0000-7000-8000-000000000001',
    'branch_id': '00000000-0000-7000-8000-000000000002',
    'shift_id': '00000000-0000-7000-8000-000000000003',
    'sita': '00000000-0000-7000-8000-000000000011',
    'ram': '00000000-0000-7000-8000-000000000012',
    'device_id': '00000000-0000-7000-8000-000000000021',
}

_FROM = DateRange(date(2026, 1, 1), None)


def _add(model, rows: list) -> None:
    """Insert rows, skipping any that already exist (INSERT ... ON CONFLICT DO NOTHING)."""
    model.objects.bulk_create(rows, ignore_conflicts=True)


@transaction.atomic
def seed_demo(*, calendar: bool = True, defaults: bool = True) -> dict:
    c, b, sh = DEMO['company_id'], DEMO['branch_id'], DEMO['shift_id']
    people = (DEMO['sita'], DEMO['ram'])
    _add(Company, [Company(id=c, code='DEMO', name_en='Demo Company', name_ne='डेमो कम्पनी')])
    _add(Branch, [Branch(id=b, company_id=c, code='HQ', name_en='Head Office')])
    _add(Shift, [Shift(id=sh, company_id=c, code='GEN', name_en='General 10-18', start_time=time(10), end_time=time(18),
                       grace_in_min=15, early_in_window_min=60, late_out_window_min=120, half_day_threshold_min=240,
                       absent_threshold_min=120)])
    _add(Employee, [Employee(id=DEMO['sita'], company_id=c, emp_code='E001', name_en='Sita Sharma', name_ne='सीता शर्मा',
                             gender='female'),
                    Employee(id=DEMO['ram'], company_id=c, emp_code='E002', name_en='Ram Thapa', name_ne='राम थापा',
                             gender='male')])
    _add(EmployeeAssignment, [EmployeeAssignment(employee_id=e, valid=_FROM, branch_id=b, employment_type='regular')
                              for e in people])
    _add(ShiftAssignment, [ShiftAssignment(employee_id=e, shift_id=sh, valid=_FROM) for e in people])
    _add(Device, [Device(id=DEMO['device_id'], company_id=c, branch_id=b, serial_no='DEMO-01', vendor='zkteco',
                         protocol='adms', name='Front door (simulator)', status='active')])
    _add(DeviceUserMap, [DeviceUserMap(device_id=DEMO['device_id'], device_user_pin=str(i), employee_id=e, valid=_FROM)
                         for i, e in enumerate(people, start=1)])
    out: dict = {}
    if calendar:
        out['bsCalendar'] = seed_bs_calendar()
    if defaults:
        out['defaults'] = seed_defaults(DEMO['company_id'])
    return out
