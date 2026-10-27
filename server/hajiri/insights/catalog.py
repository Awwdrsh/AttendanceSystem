"""
The report catalogue at /reports (port of apps/web/src/pages/hr/ReportsPage.tsx). Each entry is one of:

- csv: inputs shown inline and an "Export CSV" button (the attendance reports of hajiri.reports.service);
- table: "View" opens the report on screen with its own inputs and branch/department/status filters, plus a CSV of
  the same view (People record reports, and reports other modules own);
- link: the report lives on another page.

Reports owned by other modules are looked up at run time (TABLE_SOURCES / CSV_SOURCES) so the catalogue never imports
a module that is not there: an entry whose source is missing is left out.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Callable

from hajiri.reports.csv import Column, Table

CATEGORIES = ('Attendance', 'Leave', 'Payroll', 'People', 'Time', 'Expenses', 'Hiring', 'Learning')


@dataclass(frozen=True)
class Param:
    """What a viewable report asks for before it runs."""
    kind: str                     # 'date' | 'range' | 'days' | 'select' | 'placement'
    key: str = ''
    label: str = ''
    max: int = 0
    default: Any = None
    options: tuple[tuple[str, str], ...] = ()
    status: bool = False          # placement: also the Current / Left / Everyone choice


@dataclass(frozen=True)
class Entry:
    key: str
    category: str
    icon: str
    title: str
    desc: str
    feature: str | None = None
    # csv: the report name for hajiri.reports.service.attendance_report, or 'source:<key>' for CSV_SOURCES
    csv: str | None = None
    csv_inputs: tuple[str, ...] = ()       # 'month', 'date', 'range', 'employee'
    csv_file: str = ''                     # file name pattern: {date} {from} {to} {y} {m} {emp}
    # table: 'people:<name>' or a TABLE_SOURCES key
    table: str | None = None
    params: tuple[Param, ...] = ()
    file: str = ''
    link: tuple[str, str] | None = None    # (url, label)

    @property
    def text(self) -> str:
        return f'{self.title} {self.desc} {self.category}'.lower()


def people(name: str) -> str:
    return f'people:{name}'


RANGE = Param('range')
PLACEMENT = Param('placement')
PLACEMENT_STATUS = Param('placement', status=True)

ENTRIES: tuple[Entry, ...] = (
    # Attendance
    Entry('register', 'Attendance', 'calendar-stats', 'Monthly attendance register',
          'One row per employee per day with arrival, departure, hours, overtime, leave and breaks. Ready for a labour inspection.',
          csv='register', csv_inputs=('month',), csv_file='register-{y}-{m}'),
    Entry('daily', 'Attendance', 'calendar-event', 'Daily attendance',
          'Status, clock-in and clock-out, late minutes and overtime for every active employee on one day.',
          csv='daily', csv_inputs=('date',), csv_file='daily-{date}'),
    Entry('ot', 'Attendance', 'clock-plus', 'Overtime register', 'Detected and approved overtime hours by day type and rate.',
          csv='ot', csv_inputs=('range',), csv_file='ot-{from}_{to}'),
    Entry('late', 'Attendance', 'clock-hour-4', 'Late arrivals and early exits', 'Count and minutes per employee across the date range.',
          csv='late-early', csv_inputs=('range',), csv_file='late-early-{from}_{to}'),
    Entry('exceptions', 'Attendance', 'alert-triangle', 'Exceptions summary',
          'Missing punches, absences, late arrivals and overtime flags per day.',
          csv='exceptions-summary', csv_inputs=('range',), csv_file='exceptions-{from}_{to}'),
    Entry('breaks', 'Attendance', 'coffee', 'Breaks',
          'Every break with its type, start, end, minutes and whether it ran over the limit (up to 92 days).',
          feature='field', csv='source:breaks', csv_inputs=('range',), csv_file='breaks-{from}_{to}'),
    Entry('visits', 'Attendance', 'map-pin', 'Field visits',
          'Client and site visits with place, purpose, arrival, departure, outcome and location (up to 92 days).',
          feature='field', csv='source:visits', csv_inputs=('range',), csv_file='field-visits-{from}_{to}'),
    Entry('devices', 'Attendance', 'device-desktop', 'Device health',
          'Last seen, firmware, punches today and offline status for each terminal.', csv='devices', csv_file='devices'),
    Entry('punches', 'Attendance', 'fingerprint', 'Punch audit for one employee',
          'Every raw punch with its device, source and any corrections that replaced it.',
          csv='punch-audit', csv_inputs=('employee', 'range'), csv_file='punches-{emp}'),
    # Leave
    Entry('leave', 'Leave', 'beach', 'Leave balances and liability', 'Balance per leave type, with encashment liability at current basic pay.',
          csv='leave-balances', csv_file='leave-balances'),
    # Payroll
    Entry('salary-sheet', 'Payroll', 'cash', 'Salary sheet and SSF sheet',
          'Every pay run has its salary sheet (on screen, Excel and print) and its SSF contribution sheet.',
          link=('/payroll', 'Open pay runs')),
    # People
    Entry('headcount-on-date', 'People', 'users', 'Headcount on a date',
          'People employed on any date, by branch, department, employment type and gender.',
          table=people('headcount-on-date'), params=(Param('date', 'date', 'On'), PLACEMENT), file='headcount'),
    Entry('statistics', 'People', 'chart-bar', 'Workforce statistics', 'Age, length of service, gender, marital status and where people come from.',
          table=people('statistics'), params=(PLACEMENT_STATUS,), file='people-statistics'),
    Entry('joiners-leavers', 'People', 'user-plus', 'Joiners and leavers',
          'Everyone who joined or left in a period, with their department and the reason for leaving.',
          table=people('joiners-leavers'), params=(RANGE, PLACEMENT), file='joiners-leavers'),
    Entry('birthdays', 'People', 'cake', 'Birthdays and work anniversaries', 'Who has a birthday or a work anniversary in the coming days.',
          table=people('birthdays-anniversaries'),
          params=(Param('date', 'from', 'From'), Param('days', 'days', 'Days ahead', max=92, default=30), PLACEMENT),
          file='birthdays-anniversaries'),
    Entry('nominees', 'People', 'users-group', 'Nominees', 'Nominees for SSF, gratuity and insurance, with their share.',
          table=people('nominees'), params=(PLACEMENT_STATUS,), file='nominees'),
    Entry('emergency', 'People', 'heartbeat', 'Emergency contacts', 'Who to call for each employee, with blood group.',
          table=people('emergency-contacts'), params=(PLACEMENT_STATUS,), file='emergency-contacts'),
    Entry('addresses', 'People', 'home', 'Addresses', 'Permanent and temporary addresses with province and district.',
          table=people('addresses'), params=(PLACEMENT_STATUS,), file='addresses'),
    Entry('education', 'People', 'school', 'Education', 'Every qualification on file: level, degree, institution and year.',
          table=people('education'), params=(PLACEMENT_STATUS,), file='education'),
    Entry('licences', 'People', 'certificate', 'Licence and document register',
          'Passports, driving licences, citizenship and documents with an expiry date. Numbers are never shown.',
          table=people('licence-register'),
          params=(Param('days', 'withinDays', 'Expiring within (days)', max=3650, default=90), PLACEMENT_STATUS),
          file='licence-register'),
    Entry('completeness', 'People', 'list-check', 'Record completeness',
          'How complete each employee record is and what is missing, least complete first.',
          table=people('record-completeness'), params=(PLACEMENT_STATUS,), file='record-completeness'),
    Entry('directory', 'People', 'address-book', 'Your own report',
          'Pick the columns, filters and sort you need from employees, education, family, previous jobs, career events or leave balances.',
          link=('/reports/builder', 'Build a report')),
    # Time
    Entry('time', 'Time', 'hourglass', 'Hours by project, client, person or activity', 'Tracked hours, billable hours and amount, and budget used.',
          feature='time', table='time',
          params=(RANGE, Param('select', 'groupBy', 'Group by', default='project',
                               options=(('project', 'Project'), ('client', 'Client'), ('employee', 'Person'), ('activity', 'Activity'), ('day', 'Day')))),
          file='time'),
    # Expenses
    Entry('spend', 'Expenses', 'receipt-refund', 'Expense spend', 'Approved claim amounts by category, department, month or person.',
          feature='expenses', table='expense-spend',
          params=(RANGE, Param('select', 'groupBy', 'Group by', default='category',
                               options=(('category', 'Category'), ('department', 'Department'), ('month', 'Month'), ('employee', 'Person')))),
          file='expense-spend'),
    Entry('advances', 'Expenses', 'receipt-refund', 'Outstanding travel advances', 'Advances paid for trips that are not settled yet.',
          feature='expenses', table='expense-advances', file='travel-advances'),
    Entry('pending-pay', 'Expenses', 'receipt-refund', 'Claims waiting for payment', 'Approved claims and settlements not yet paid through payroll.',
          feature='expenses', table='expense-pending-payment', file='expenses-pending-payment'),
    # Hiring and learning
    Entry('hiring', 'Hiring', 'user-plus', 'Hiring funnel and time to hire',
          'Applications reaching each stage per job, time to hire and where candidates come from.', feature='hiring',
          link=('/hiring', 'Open hiring')),
    Entry('training-cost', 'Learning', 'book', 'Training cost and certificates',
          'Training spend by course and department, completions and certificates about to expire.', feature='learning',
          link=('/performance/learning', 'Open learning')),
)

ENTRY_BY_KEY = {e.key: e for e in ENTRIES}

# Reports other modules own: (module path, function name). Each function takes (user, query) with the TS query names
# and returns a hajiri.reports.csv.Table or a dict {'columns': [{'key', 'header', 'type'?}], 'rows': [...]}.
TABLE_SOURCES: dict[str, tuple[str, str]] = {
    'time': ('hajiri.timesheets.services', 'report_summary'),                      # GET /time/reports/summary
    'expense-spend': ('hajiri.expenses.services', 'report_spend'),                # GET /expenses/reports/spend
    'expense-advances': ('hajiri.expenses.services', 'report_advances'),          # GET /expenses/reports/advances
    'expense-pending-payment': ('hajiri.expenses.services', 'report_pending_payment'),  # GET /expenses/reports/pending-payment
}
# CSV-only reports other modules own; same contract, called with {'from', 'to', 'format': 'csv'}.
CSV_SOURCES: dict[str, tuple[str, str]] = {
    'breaks': ('hajiri.field.services', 'list_breaks'),    # GET /field/breaks?format=csv
    'visits': ('hajiri.field.services', 'list_visits'),    # GET /field/visits?format=csv
}


def resolve(spec: tuple[str, str] | None) -> Callable | None:
    if not spec:
        return None
    try:
        return getattr(import_module(spec[0]), spec[1], None)
    except ImportError:
        return None


def source_for(entry: Entry) -> Callable | None:
    """The function that runs an entry from another module, or None when that module does not provide it (yet)."""
    if entry.table and not entry.table.startswith('people:'):
        return resolve(TABLE_SOURCES.get(entry.table))
    if entry.csv and entry.csv.startswith('source:'):
        return resolve(CSV_SOURCES.get(entry.csv[len('source:'):]))
    return None


def call_source(fn: Callable, user, query: dict):
    """Run a module's report: (user, query), or (user) for reports that take no inputs."""
    try:
        n = len(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        n = 2
    return fn(user, query) if n >= 2 else fn(user)


def available(entry: Entry) -> bool:
    if (entry.table and not entry.table.startswith('people:')) or (entry.csv and entry.csv.startswith('source:')):
        return source_for(entry) is not None
    return True


def as_table(result: Any, filename: str) -> Table:
    """A module's report result as a Table (columns with types, rows), named for the download."""
    if isinstance(result, Table):
        return Table(result.columns, result.rows, filename)
    cols = [c if isinstance(c, Column) else Column(c['key'], c['header'], c.get('type')) for c in result['columns']]
    return Table(cols, list(result['rows']), filename)
