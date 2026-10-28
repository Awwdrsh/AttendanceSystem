"""The batched lookups behind the roster, the swap picker and the penalty preview give the same answers as the one-row
rules they replace (effective_shift per day, ledger_balance - pending_days per leave type)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from engines.bs_calendar import weekday_of
from hajiri.attendance_tools.services import _available_days, check_upload
from hajiri.db.models import LeaveLedger, LeaveRequest, LeaveType, Roster, Shift, ShiftAssignment
from hajiri.field.services import effective_shift, effective_shifts
from hajiri.leave.service import ledger_balance, pending_days
from tests.web.factories import make_employee

pytestmark = pytest.mark.django_db


def test_effective_shifts_match_the_rule_for_every_source(acme):
    gen = Shift.objects.create(company=acme, code='GEN', name_en='General', start_time='10:00', end_time='18:00')
    eve = Shift.objects.create(company=acme, code='EVE', name_en='Evening', start_time='14:00', end_time='22:00')
    start = date(2026, 10, 1)
    days = [(start + timedelta(days=i)).isoformat() for i in range(14)]
    off = weekday_of(days[3])
    a = make_employee(acme, weekly_off_days=[off])
    ShiftAssignment.objects.create(employee=a, shift=gen, valid=(start, start + timedelta(days=7)))      # days 0..6
    ShiftAssignment.objects.create(employee=a, shift=eve, valid=(start + timedelta(days=7), None))       # days 7..
    Roster.objects.create(employee=a, local_date=days[1], shift=eve)
    Roster.objects.create(employee=a, local_date=days[2], shift=None)
    Roster.objects.create(employee=a, local_date=days[3], shift=gen)       # a roster row beats the weekly off
    b = make_employee(acme)                                                 # no shift at all
    got = effective_shifts([a.id, b.id, '00000000-0000-0000-0000-000000000000'], days)
    sources = set()
    for e in (str(a.id), str(b.id), '00000000-0000-0000-0000-000000000000'):
        for d in days:
            assert got[(e, d)] == effective_shift(e, d) == _one_by_one(e, d), (e, d)
            sources.add(got[(e, d)]['source'])
    assert sources == {'roster', 'roster_off', 'weekly_off', 'default', 'none'}
    assert got[(str(a.id), days[1])] == {'shiftId': str(eve.id), 'source': 'roster'}
    assert {got[(str(a.id), d)]['shiftId'] for d in days[8:] if got[(str(a.id), d)]['source'] == 'default'} == {str(eve.id)}


def _one_by_one(employee_id: str, local_date: str) -> dict:
    """The rule as the port wrote it, one query per person and day."""
    from hajiri.db.models import Employee
    day = date.fromisoformat(local_date)
    e = Employee.objects.filter(pk=employee_id).values('weekly_off_days').first()
    if not e:
        return {'shiftId': None, 'source': 'none'}
    r = Roster.objects.filter(employee_id=employee_id, local_date=day).values('shift_id').first()
    if r:
        return {'shiftId': str(r['shift_id']), 'source': 'roster'} if r['shift_id'] else {'shiftId': None, 'source': 'roster_off'}
    if weekday_of(local_date) in (e['weekly_off_days'] or []):
        return {'shiftId': None, 'source': 'weekly_off'}
    s = ShiftAssignment.objects.filter(employee_id=employee_id, valid__contains=day).values_list('shift_id', flat=True).first()
    return {'shiftId': str(s), 'source': 'default'} if s else {'shiftId': None, 'source': 'none'}


def test_available_days_match_ledger_balance_minus_pending(acme):
    al = LeaveType.objects.create(company=acme, code='AL', name_en='Annual', paid=True)
    sl = LeaveType.objects.create(company=acme, code='SL', name_en='Sick', paid=True)
    a, b = make_employee(acme), make_employee(acme)
    d = date(2026, 10, 1)
    for e, t, n in ((a, al, 10), (a, al, -1.5), (a, sl, 4), (b, sl, 2.25)):
        LeaveLedger.objects.create(employee=e, leave_type=t, effective_date=d, delta_days=n, reason=LeaveLedger.Reason.ALLOCATION)
    LeaveRequest.objects.create(employee=a, leave_type=al, from_date=d, to_date=d, days_requested=1, status='pending')
    # Sick leave whose bridged weekend is paid from annual leave: 2 days on SL, 1.5 on AL
    LeaveRequest.objects.create(employee=a, leave_type=sl, from_date=d, to_date=d, days_requested=2, bridge_type=al, bridge_days=1.5,
                                status='pending')
    LeaveRequest.objects.create(employee=a, leave_type=al, from_date=d, to_date=d, days_requested=3, status='approved')
    LeaveRequest.objects.create(employee=b, leave_type=al, from_date=d, to_date=d, days_requested=0.5, status='pending')
    emps, types = [str(a.id), str(b.id)], [str(al.id), str(sl.id)]
    got = _available_days(emps, types)
    for e in emps:
        for t in types:
            assert got[(e, t)] == ledger_balance(e, t) - pending_days(e, t), (e, t)
    assert got[(str(a.id), str(al.id))] == 10 - 1.5 - 1 - 1.5
    assert _available_days([], types) == {} and _available_days(emps, []) == {}


def test_upload_refuses_an_ad_date_that_does_not_exist(acme):
    make_employee(acme, code='E77')
    rows = check_upload(acme.id, 'emp_code,date_ad,in_time\nE77,2026-02-30,09:00\nE77,2026-02-28,09:00\n')['rows']
    assert rows[0]['errors'] == ['The date is missing or not YYYY-MM-DD.'] and rows[0]['date'] is None
    assert rows[1]['date'] == '2026-02-28'
