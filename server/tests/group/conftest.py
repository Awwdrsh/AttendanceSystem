"""
The multicompany `world` (Alpha and Beta in one group, Gamma outside it) plus figures in every company, so group
views can be checked for correct sums and for never showing Gamma.

The world's employees: Alpha has 4 active (Asha Admin, Sita Solo, Dipak Dual, Kiran Group Exec), Beta 3 (Bimal Admin,
Dipak at Beta, Gopal Group HR), Gamma 1 (Gita Admin). `figures` adds, in Alpha and Beta:

    Alpha  + a joiner this month, a leaver this month (exited), a probation review due
           attendance today: 2 in (1 late), 1 absent; 1 pending approval; 90 min approved overtime this month
           a locked regular run last month: payslips gross 50,000 + 30,000, employer 10,000 + 6,000,
           employee statutory 5,500 + 3,300, TDS 1,000 + 0, net 43,500 + 26,700
           leave: 10 days allocated, 2 taken this month
    Beta   + an employee on approved leave today, one offline clock-in device, one expiring document
           attendance today: 1 in; 2 pending approvals
           a locked regular run last month: one payslip gross 40,000, employer 8,000, employee 4,400, TDS 500,
           net 35,100
and the same kinds of figures, much larger, in Gamma (which must never be counted).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest

from hajiri.core.clock import npt_today
from hajiri.db.models import (
    AttendanceDay, Device, DocumentCategory, Employee, EmployeeDocument, LeaveLedger, LeaveRequest, LeaveType,
    OtSlip, PayrollPeriod, PayrollRun, Payslip, StoredFile, WorkflowInstance,
)
from hajiri.group import services
from tests.multicompany.conftest import World, world  # noqa: F401  (the fixture)
from tests.web.factories import make_employee


@pytest.fixture(autouse=True)
def _no_dashboard_cache(settings):
    settings.GROUP_DASHBOARD_CACHE_SECONDS = 0


@dataclass
class Figures:
    today: date
    this: services.Month
    prev: services.Month
    joiner: Employee
    leaver: Employee
    beta_female: Employee


def _day(e, d, status, late=0):
    return AttendanceDay.objects.create(employee=e, local_date=d, version=1, status=status, late_min=late, inputs_hash='t')


def _pending(company, n):
    for _ in range(n):
        WorkflowInstance.objects.create(company=company, object_type='test', object_id=uuid.uuid4(), steps=[])


def _run(company, m: services.Month, slips: list[tuple]) -> PayrollRun:
    period = PayrollPeriod.objects.create(company=company, bs_year=m.year, bs_month=m.month, start_ad=m.start,
                                          end_ad=m.end, days=(m.end - m.start).days + 1)
    run = PayrollRun.objects.create(company=company, period=period)
    for emp, gross, employer, employee_st, tds, net in slips:
        Payslip.objects.create(run=run, employee=emp, inputs_snapshot={}, bs_days_in_month=30, paid_days=30,
                               gross=gross, taxable=gross, tds=tds, employee_statutory=employee_st,
                               employer_statutory=employer, total_deductions=Decimal(gross) - Decimal(net), net=net)
    PayrollRun.objects.filter(pk=run.pk).update(status=PayrollRun.Status.LOCKED)
    return run


def _leave_type(company):
    return LeaveType.objects.create(company=company, code='GTEST', name_en='Test leave')


def _doc(company, emp, expiry):
    cat = DocumentCategory.objects.create(company=company, code='gtest_passport', name='Passport')
    f = StoredFile.objects.create(company=company, purpose='employee_document', filename='p.pdf', mime='application/pdf',
                                  bytes=1, sha256='0' * 64)
    EmployeeDocument.objects.create(company=company, employee=emp, category=cat, title='Passport', file=f,
                                    expiry_date=expiry)


@pytest.fixture
def figures(world: World) -> Figures:
    today = npt_today()
    this = services.current_month(today)
    prev = services.month(*services._prev(this.year, this.month))
    alpha, beta, gamma = world.alpha, world.beta, world.gamma
    a = list(Employee.objects.filter(company=alpha).order_by('created_at'))
    b = list(Employee.objects.filter(company=beta).order_by('created_at'))
    g = list(Employee.objects.filter(company=gamma).order_by('created_at'))
    year_ago = today - timedelta(days=400)
    Employee.objects.filter(company__in=[alpha, beta, gamma]).update(join_date=year_ago)

    # Alpha: a joiner, a leaver and a probation review due.
    joiner = make_employee(alpha, name='Jamuna Joiner', join_date=this.start, gender='female')
    leaver = make_employee(alpha, name='Lalit Leaver', join_date=year_ago, status=Employee.Status.EXITED,
                           exit_date=this.start)
    make_employee(alpha, name='Pawan Probation', join_date=today - timedelta(days=170),
                  probation_end=today + timedelta(days=10))
    # Beta: someone on leave today, of a different gender and employment type.
    beta_female = make_employee(beta, name='Bina On Leave', join_date=year_ago, gender='female', employment_type='part_time')

    # Attendance today.
    _day(a[0], today, AttendanceDay.Status.PRESENT)
    _day(a[1], today, AttendanceDay.Status.PRESENT, late=12)
    _day(a[2], today, AttendanceDay.Status.ABSENT)
    _day(b[0], today, AttendanceDay.Status.PRESENT)
    for e in g:
        _day(e, today, AttendanceDay.Status.ABSENT)

    _pending(alpha, 1)
    _pending(beta, 2)
    _pending(gamma, 5)

    # Leave today in Beta (and Gamma).
    lt_b, lt_g, lt_a = _leave_type(beta), _leave_type(gamma), _leave_type(alpha)
    LeaveRequest.objects.create(employee=beta_female, leave_type=lt_b, from_date=today, to_date=today, days_requested=1,
                                status=LeaveRequest.Status.APPROVED)
    LeaveRequest.objects.create(employee=g[0], leave_type=lt_g, from_date=today, to_date=today, days_requested=1,
                                status=LeaveRequest.Status.APPROVED)
    # Alpha leave ledger: 10 allocated last year, 2 taken this month.
    LeaveLedger.objects.create(employee=a[0], leave_type=lt_a, effective_date=year_ago, delta_days=10,
                               reason=LeaveLedger.Reason.ALLOCATION)
    LeaveLedger.objects.create(employee=a[0], leave_type=lt_a, effective_date=this.start, delta_days=-2,
                               reason=LeaveLedger.Reason.USAGE)
    LeaveLedger.objects.create(employee=g[0], leave_type=lt_g, effective_date=this.start, delta_days=-7,
                               reason=LeaveLedger.Reason.USAGE)

    # Overtime this month.
    OtSlip.objects.create(employee=a[0], local_date=this.start, detected_min=120, approved_min=90,
                          day_type=OtSlip.DayType.WORKING, status=OtSlip.Status.APPROVED)
    OtSlip.objects.create(employee=g[0], local_date=this.start, detected_min=600, day_type=OtSlip.DayType.WORKING)

    # Devices and documents.
    Device.objects.create(company=beta, serial_no='BETA-OFF-1', vendor=Device.Vendor.ZKTECO, protocol=Device.Protocol.ADMS,
                          status=Device.Status.ACTIVE, name='Beta gate')
    Device.objects.create(company=gamma, serial_no='GAMMA-OFF-1', vendor=Device.Vendor.ZKTECO,
                          protocol=Device.Protocol.ADMS, status=Device.Status.ACTIVE)
    _doc(beta, b[1], today + timedelta(days=5))
    _doc(gamma, g[0], today + timedelta(days=5))

    # Payroll last month.
    _run(alpha, prev, [(a[0], '50000.00', '10000.00', '5500.00', '1000.00', '43500.00'),
                       (a[1], '30000.00', '6000.00', '3300.00', '0.00', '26700.00')])
    _run(beta, prev, [(b[0], '40000.00', '8000.00', '4400.00', '500.00', '35100.00')])
    _run(gamma, prev, [(g[0], '999999.00', '1.00', '1.00', '1.00', '999996.00')])
    return Figures(today, this, prev, joiner, leaver, beta_female)
