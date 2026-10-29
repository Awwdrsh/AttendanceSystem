"""Query counts of the slice's list pages stay flat as the company grows (no query per row)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from engines.bs_calendar import ad_to_bs, bs_month_bounds
from hajiri.core.clock import npt_today
from hajiri.db.models import (
    AttendanceDay, FieldVisit, Kiosk, KioskCredential, LeaveLedger, LeaveType, Location, LocationAssignment, Roster, Shift,
    ShiftAssignment, WorkBreak,
)
from tests.web.factories import make_employee, make_user, sign_in

pytestmark = pytest.mark.django_db


def _grow(company, boss, shift, lt, n: int, start: int) -> None:
    """n more people reporting to `boss`, each with a location, a kiosk PIN, late days, a break, a visit and rosters."""
    from django.db.models.functions import Now
    today = npt_today()
    loc = Location.objects.create(company=company, name=f'Site {start}', lat=27.7, lng=85.3, radius_m=150, active=True)
    b = bs_month_bounds(*_bs_month())
    for i in range(start, start + n):
        e = make_employee(company, name=f'Person {i:03d}', manager=boss)
        LocationAssignment.objects.create(location=loc, employee=e, company=company)
        KioskCredential.objects.create(employee=e, company=company, pin_hash='x')
        ShiftAssignment.objects.create(employee=e, shift=shift, valid=(today - timedelta(days=400), None))
        for d in (0, 1):
            day = b['startAd'] if d == 0 else b['endAd']
            AttendanceDay.objects.create(employee=e, local_date=day, version=1, status=AttendanceDay.Status.PRESENT, late_min=20,
                                         inputs_hash='h')
        WorkBreak.objects.create(company=company, employee=e, local_date=today, kind='tea', paid=True, max_min=15,
                                 started_at=Now(), ended_at=Now(), minutes=5)
        FieldVisit.objects.create(company=company, employee=e, local_date=today, place='Client', started_at=Now(), ended_at=Now())
        Roster.objects.create(employee=e, local_date=today + timedelta(days=1), shift=shift)
        LeaveLedger.objects.create(employee=e, leave_type=lt, effective_date=today, delta_days=5, reason=LeaveLedger.Reason.ALLOCATION)
    Kiosk.objects.create(company=company, name=f'Gate {start}', selfie_required=False)


def _bs_month() -> tuple[int, int]:
    t = ad_to_bs(npt_today().isoformat())
    return t.year, t.month


def _count(client, url: str) -> int:
    client.get(url)                 # warm the per-session caches first
    with CaptureQueriesContext(connection) as ctx:
        r = client.get(url)
    assert r.status_code == 200, (url, r.status_code, r.content[:300])
    # the session row is touched at most once a minute, so leave it out of the comparison
    return sum(1 for q in ctx.captured_queries if not q['sql'].startswith('UPDATE "session"'))


@pytest.fixture
def setup(acme, client):
    from hajiri.attendance_tools.services import EXTRAS_KEY, default_extras
    from hajiri.core.settings_store import put_setting
    boss = make_employee(acme, name='Boss')
    shift = Shift.objects.create(company=acme, code='GEN', name_en='General', start_time='10:00', end_time='18:00')
    lt = LeaveType.objects.create(company=acme, code='AL', name_en='Annual', paid=True)
    policy = default_extras()
    policy['latePenalty'].update(enabled=True, lateCount=1, leaveTypeIds=[str(lt.id)])
    put_setting(acme.id, EXTRAS_KEY, policy)
    hr = make_user(acme, roles=['hr'])
    sign_in(client, hr)
    return acme, boss, shift, lt


# What the HR pages load: locations, kiosks, field visits and breaks, attendance issues, late penalties, from home.
PAGES = ['/api/v1/field/locations', '/api/v1/field/kiosks', '/api/v1/field/visits?from={from}&to={to}',
         '/api/v1/field/breaks?from={from}&to={to}', '/api/v1/attendance-tools/issues?from={from}&to={to}',
         '/api/v1/attendance-tools/penalties?bsYear={y}&bsMonth={m}', '/api/v1/punch/remote?from={from}&to={to}']


def _fill(url: str) -> str:
    today = npt_today()
    bs = ad_to_bs(today)
    return url.format(**{'from': (today - timedelta(days=20)).isoformat(), 'to': today.isoformat(), 'y': bs.year, 'm': bs.month})


@pytest.mark.parametrize('url', PAGES)
def test_hr_list_pages_do_not_query_per_row(setup, client, url):
    acme, boss, shift, lt = setup
    url = _fill(url)
    _grow(acme, boss, shift, lt, 2, 0)
    small = _count(client, url)
    _grow(acme, boss, shift, lt, 6, 100)
    assert _count(client, url) == small


@pytest.mark.parametrize('url', ['/api/v1/field/my-roster?from={from}&to={to}', '/api/v1/field/swap-colleagues?date={to}'])
def test_my_roster_and_swap_picker_do_not_query_per_day_or_colleague(setup, url):
    from django.test import Client
    acme, boss, shift, lt = setup
    me = make_employee(acme, name='Me', manager=boss)
    ShiftAssignment.objects.create(employee=me, shift=shift, valid=(npt_today() - timedelta(days=400), None))
    c = Client()
    sign_in(c, make_user(acme, roles=['employee'], employee=me))
    url = _fill(url)
    _grow(acme, boss, shift, lt, 2, 0)
    small = _count(c, url)
    _grow(acme, boss, shift, lt, 6, 100)
    assert _count(c, url) == small

