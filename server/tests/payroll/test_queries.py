"""The payroll list pages read a fixed number of queries however many rows they show (no query per row)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from psycopg.types.range import DateRange

from hajiri.core.auth import SESSION_COOKIE
from hajiri.db import models as m
from tests.web.factories import make_employee

LOCKED = datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc)


def _client(token: str) -> Client:
    c = Client()
    c.cookies[SESSION_COOKIE] = token
    return c


def _count(c: Client, url: str) -> int:
    c.get(url)      # warm up: the first request of a session also records when it was last seen
    with CaptureQueriesContext(connection) as ctx:
        r = c.get(url)
    assert r.status_code == 200, (url, r.status_code)
    return len(ctx.captured_queries)


class Data:
    """Adds one more row of everything the list pages show, for a new employee of the company."""

    def __init__(self, company_id: str, employee_id: str):
        self.company = m.Company.objects.get(pk=company_id)
        self.employee_id = employee_id
        self.version = m.SalaryStructureVersion.objects.for_company(company_id).order_by('version').first()
        self.n = 0

    def add(self, k: int = 1) -> None:
        for _ in range(k):
            self.n += 1
            n = self.n
            emp = make_employee(self.company, code=f'Q{n:03d}', name=f'Query {n}')
            period = m.PayrollPeriod.objects.create(company=self.company, bs_year=2083, bs_month=3 + n, start_ad=date(2013 + n, 10, 18),
                                                    end_ad=date(2013 + n, 11, 16), days=30)
            run = m.PayrollRun.objects.create(company=self.company, period=period, status=m.PayrollRun.Status.LOCKED, locked_at=LOCKED,
                                              totals={'headcount': '1', 'gross': '50000.00', 'net': '49500.00'})
            z = Decimal('0')
            for eid in (emp.id, self.employee_id):
                slip = m.Payslip.objects.create(run=run, employee_id=eid, inputs_snapshot={'input': {}, 'result': {}}, bs_days_in_month=30,
                                                paid_days=Decimal('30'), gross=Decimal('50000'), taxable=Decimal('50000'), tds=Decimal('500'),
                                                total_deductions=Decimal('500'), net=Decimal('49500'), lop_days=z, ot_hours=z,
                                                published_at=LOCKED)
                m.PayslipLine.objects.create(payslip=slip, component_code='BASIC', type='earning', name_en='Basic salary',
                                             amount=Decimal('50000'), sort_order=10)
            m.PayrollHold.objects.create(company=self.company, employee=emp, run=run, amount=Decimal('49500'), reason='documents pending')
            loan = m.Loan.objects.create(employee=emp, principal=Decimal('10000'), emi=Decimal('1000'), start_bs_year=2083, start_bs_month=1,
                                         installments=10)
            m.LoanInstallment.objects.create(loan=loan, payslip=slip, amount=Decimal('1000'))
            m.SalaryAssignment.objects.create(employee=emp, structure_version=self.version, valid=DateRange(date(2026, 1, 1), None),
                                              base_amounts={'BASIC': '50000.00'})
            m.PayrollOpeningBalance.objects.create(company=self.company, employee=emp, fiscal_year_start=2083, gross=Decimal('100000'),
                                                   taxable=Decimal('100000'))
            s = m.SalaryStructure.objects.create(company=self.company, code=f'Q{n}', name_en=f'Structure {n}')
            m.SalaryStructureVersion.objects.create(structure=s, version=1, components=[{'code': 'BASIC'}])
            m.SalaryStructureVersion.objects.create(structure=s, version=2, components=[{'code': 'BASIC'}])


@pytest.fixture
def setup(demo, login_as):
    from hajiri.payroll.seeds import seed_payroll_defaults
    seed_payroll_defaults(demo['company_id'])
    hr = login_as(['hr'])
    sita = login_as(['employee'], demo['sita'])
    return Data(demo['company_id'], demo['sita']), _client(hr.token), _client(sita.token)


HR_PAGES = ('/payroll', '/payroll/loans', '/payroll/assignments', '/payroll/held-pay?tab=held', '/payroll/opening-balances?fy=2083',
            '/payroll/structures?tab=structures', '/payroll/structures', '/payroll/rules')


def test_hr_list_pages_do_not_query_per_row(setup):
    data, hr, _ = setup
    data.add(1)
    before = {url: _count(hr, url) for url in HR_PAGES}
    data.add(4)
    after = {url: _count(hr, url) for url in HR_PAGES}
    assert after == before


def test_own_payslips_and_run_page_do_not_query_per_row(setup):
    data, hr, sita = setup
    data.add(1)
    run = m.PayrollRun.objects.for_company(data.company.id).order_by('created_at').first()
    pages = ('/me/payslips?fy=2083', '/me/payslips/tax')
    sheet = f'/payroll/runs/{run.id}?step=4'
    before = [_count(sita, url) for url in pages] + [_count(hr, sheet)]
    for _ in range(4):
        # More payslips in the same run, for more employees.
        emp = make_employee(data.company)
        z = Decimal('0')
        slip = m.Payslip.objects.create(run=run, employee=emp, inputs_snapshot={'input': {}, 'result': {}}, bs_days_in_month=30,
                                        paid_days=Decimal('30'), gross=Decimal('50000'), taxable=Decimal('50000'), tds=Decimal('500'),
                                        total_deductions=Decimal('500'), net=Decimal('49500'), lop_days=z, ot_hours=z)
        m.PayslipLine.objects.create(payslip=slip, component_code='BASIC', type='earning', name_en='Basic salary', amount=Decimal('50000'),
                                     sort_order=10)
    data.add(2)
    after = [_count(sita, url) for url in pages] + [_count(hr, sheet)]
    assert after == before
    assert m.Payslip.objects.filter(employee_id=data.employee_id, run__period__bs_year=2083).count() == 3   # the list really grew
    assert m.Payslip.objects.filter(run=run).count() == 6
