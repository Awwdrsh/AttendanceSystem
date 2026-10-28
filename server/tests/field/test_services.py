"""The ORM versions of the break-closing statements: a stale break ends at the day's first clock-out after it began
(capped at its limit), and a kiosk clock-out closes an open break at the punch time."""
from __future__ import annotations

from datetime import timedelta

from hajiri.core.clock import now, npt_today
from hajiri.db.models import PunchEvent, WorkBreak
from hajiri.field.services import close_open_break, close_stale_breaks, effective_shift
from tests.web.factories import make_company, make_employee


def _break(co, emp, *, started, max_min, day):
    return WorkBreak.objects.create(company=co, employee=emp, local_date=day, kind=WorkBreak.Kind.LUNCH, paid=False,
                                    max_min=max_min, started_at=started)


def test_a_stale_break_ends_at_the_first_clock_out_after_it(db):
    co = make_company()
    emp = make_employee(co)
    day = npt_today() - timedelta(days=1)
    start = now() - timedelta(days=1, hours=3)
    b = _break(co, emp, started=start, max_min=60, day=day)
    PunchEvent.objects.create(device_sn='X', vendor='kiosk', device_user_pin='1', employee=emp, punched_at=start + timedelta(minutes=20),
                              device_local_time='x', local_date=day, direction='out', verify_mode='kiosk', source='kiosk',
                              idempotency_key='stale-out-1')
    assert close_stale_breaks() == 1
    b.refresh_from_db()
    assert b.ended_at == start + timedelta(minutes=20) and b.minutes == 20 and b.over_limit is False
    assert close_stale_breaks() == 0


def test_a_break_left_open_today_is_not_closed_and_a_kiosk_clock_out_closes_it(db):
    co = make_company()
    emp = make_employee(co)
    start = now() - timedelta(minutes=50)
    b = _break(co, emp, started=start, max_min=30, day=npt_today())
    assert close_stale_breaks() == 0
    close_open_break(str(emp.id), start + timedelta(minutes=45))
    b.refresh_from_db()
    assert b.minutes == 45 and b.over_limit is True
    # a clock-out before the break started never gives a negative break
    b2 = _break(co, make_employee(co), started=start, max_min=30, day=npt_today())
    close_open_break(str(b2.employee_id), start - timedelta(minutes=5))
    b2.refresh_from_db()
    assert b2.ended_at == start and b2.minutes == 0


def test_effective_shift_without_assignment(db):
    co = make_company()
    emp = make_employee(co, weekly_off_days=[])
    assert effective_shift(str(emp.id), npt_today().isoformat()) == {'shiftId': None, 'source': 'none'}
