"""Query budgets of the shifts lists: the shifts page, the holidays page, the roster page and a roster save take the
same number of queries for 3 people as for 12 (no query per row), and branch ids of another company are refused."""
from __future__ import annotations

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from hajiri.core.errors import ForbiddenError, NotFoundError
from hajiri.db.models import Branch, Company, Holiday, Roster, Shift
from hajiri.shifts import services as svc
from hajiri.shifts.forms import HolidayImportForm
from tests.web.factories import make_company, make_employee, make_user, sign_in

WEEK = '2026-11-01'


def _people(company, shift_id, hr, n: int) -> list[str]:
    ids = []
    for _ in range(n):
        e = make_employee(company)
        svc.assign_shift(hr, {'employeeId': str(e.id), 'shiftId': shift_id, 'from': '2026-10-01'})
        ids.append(str(e.id))
    svc.save_roster(hr, {'rows': [{'employeeId': i, 'localDate': f'2026-11-0{d}', 'shiftId': shift_id} for i in ids for d in (2, 3)]})
    return ids


def _count(fn) -> int:
    fn()  # warm: the first request of a session also touches the session row
    with CaptureQueriesContext(connection) as ctx:
        fn()
    return len(ctx.captured_queries)


@pytest.fixture
def setup(demo):
    company = Company.objects.get(pk=demo['company_id'])
    hr = make_user(company, roles=['hr'])
    c = Client()
    sign_in(c, hr)
    return company, hr, c


def test_list_pages_do_not_query_per_row(setup):
    company, hr, c = setup
    gen = str(Shift.objects.for_company(company.id).get(code='GEN').pk)
    _people(company, gen, hr, 3)
    for code in ('A1', 'A2'):
        svc.create_shift(hr, {'code': code, 'nameEn': code, 'start': '06:00', 'end': '14:00'})
    for d in ('2026-12-01', '2026-12-02'):
        svc.create_holiday(hr, {'localDate': d, 'nameEn': f'Day {d}'})
    pages = ['/api/v1/shifts', '/api/v1/shifts/holidays', f'/api/v1/shifts/roster?from={WEEK}&to={WEEK}',
             f'/api/v1/shifts/calendar?from={WEEK}&to={WEEK}']
    small = {p: _count(lambda p=p: c.get(p)) for p in pages}

    _people(company, gen, hr, 9)
    for code in ('B1', 'B2', 'B3', 'B4', 'B5'):
        svc.create_shift(hr, {'code': code, 'nameEn': code, 'start': '06:00', 'end': '14:00'})
    for d in ('2026-12-03', '2026-12-04', '2026-12-05', '2026-12-06'):
        svc.create_holiday(hr, {'localDate': d, 'nameEn': f'Day {d}'})
    for p in pages:
        r = c.get(p)
        assert r.status_code == 200, p
        assert _count(lambda p=p: c.get(p)) == small[p], p


def test_roster_save_checks_the_whole_body_in_constant_queries(setup):
    company, hr, _ = setup
    gen = str(Shift.objects.for_company(company.id).get(code='GEN').pk)
    few = [str(make_employee(company).id) for _ in range(2)]
    many = [str(make_employee(company).id) for _ in range(10)]

    def body(ids, day):
        return {'rows': [{'employeeId': i, 'localDate': day, 'shiftId': gen} for i in ids]}
    n_few = _count(lambda: svc.save_roster(hr, body(few, '2026-11-05')))
    n_many = _count(lambda: svc.save_roster(hr, body(many, '2026-11-05')))
    assert n_few == n_many
    assert Roster.objects.filter(employee_id__in=many, local_date='2026-11-05').count() == 10


def test_roster_save_still_reports_the_first_problem_in_body_order(setup):
    company, hr, _ = setup
    gen = str(Shift.objects.for_company(company.id).get(code='GEN').pk)
    ok = str(make_employee(company).id)
    stranger = str(make_employee(make_company()).id)
    with pytest.raises(NotFoundError, match=f'employee {stranger} not found'):
        svc.save_roster(hr, {'rows': [{'employeeId': ok, 'localDate': '2026-11-05', 'shiftId': None},
                                      {'employeeId': stranger, 'localDate': '2026-11-05', 'shiftId': gen}]})
    missing = '00000000-0000-0000-0000-000000000001'
    with pytest.raises(NotFoundError, match=f'shift {missing} not found'):
        svc.save_roster(hr, {'rows': [{'employeeId': ok, 'localDate': '2026-11-05', 'shiftId': missing}]})
    assert not Roster.objects.filter(employee_id=ok).exists()


def test_a_manager_rosters_only_their_people(setup):
    company, hr, _ = setup
    boss = make_employee(company)
    report = make_employee(company, manager=boss)
    other = make_employee(company)
    mgr = make_user(company, roles=['manager'], employee=boss)
    assert svc.save_roster(mgr, {'rows': [{'employeeId': str(report.id), 'localDate': '2026-11-05', 'shiftId': None},
                                          {'employeeId': str(boss.id), 'localDate': '2026-11-05', 'shiftId': None}]}) == {'upserted': 2}
    with pytest.raises(ForbiddenError, match='not allowed to act for this employee'):
        svc.save_roster(mgr, {'rows': [{'employeeId': str(report.id), 'localDate': '2026-11-06', 'shiftId': None},
                                       {'employeeId': str(other.id), 'localDate': '2026-11-06', 'shiftId': None}]})
    assert not Roster.objects.filter(local_date='2026-11-06').exists()


def test_branch_of_another_company_is_not_found(setup):
    company, hr, _ = setup
    foreign = Branch.objects.create(company=make_company(), code='FX', name_en='Foreign branch')
    with pytest.raises(NotFoundError, match='branch not found'):
        svc.create_holiday(hr, {'localDate': '2026-12-20', 'nameEn': 'Foreign', 'branchId': str(foreign.id)})
    with pytest.raises(NotFoundError, match='branch not found'):
        svc.create_location(hr, {'name': 'Yard', 'lat': 27.7, 'lng': 85.3, 'branchId': str(foreign.id)})
    hid = svc.create_holiday(hr, {'localDate': '2026-12-21', 'nameEn': 'Ours'})['id']
    with pytest.raises(NotFoundError, match='branch not found'):
        svc.update_holiday(hr, hid, {'branchId': str(foreign.id)})
    assert Holiday.objects.get(pk=hid).branch_id is None
    assert not Holiday.objects.filter(name_en='Foreign').exists()


def test_import_button_year_zero_is_refused_not_read_as_2083():
    assert HolidayImportForm({}).body() == {'bsYear': 2083}
    assert HolidayImportForm({'bs_year': '0'}).body() == {'bsYear': 0}
