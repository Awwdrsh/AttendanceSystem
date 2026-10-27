"""Sample data for the component gallery at /_/web/components (no database rows needed)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .format import add_days, today_npt
from .tables import Column, Table
from .templatetags.hj import status_badge_html

PEOPLE = [
    ('E001', 'Sita Sharma', 'Engineering', date(2021, 4, 14), 'Present', 0, '185000.00'),
    ('E002', 'Ram Thapa', 'Finance', date(2019, 7, 17), 'Late', 12, '142500.50'),
    ('E003', 'Gita Gurung', 'People Ops', date(2022, 1, 3), 'OnLeave', 0, '98000.00'),
    ('E004', 'Hari Karki', 'Engineering', date(2023, 9, 1), 'Absent', 0, '76000.00'),
    ('E005', 'Maya Rai', 'Sales', date(2020, 11, 23), 'WFH', 0, '120000.00'),
    ('E006', 'Bikash Shrestha', 'Engineering', date(2018, 2, 12), 'Incomplete', 0, '210000.00'),
    ('E007', 'Anita Magar', 'Sales', date(2024, 5, 30), 'Present', 3, '64000.00'),
    ('E008', 'Suman Tamang', 'Operations', date(2017, 8, 8), 'Holiday', 0, '134000.00'),
    ('E009', 'Kiran Adhikari', 'Finance', date(2021, 12, 1), 'WeeklyOff', 0, '91000.00'),
    ('E010', 'Nabin Poudel', 'Operations', date(2025, 1, 19), 'Present', 0, '58000.00'),
    ('E011', 'Rita Bhandari', 'People Ops', date(2016, 3, 21), 'HalfDayLeave', 0, '156000.00'),
    ('E012', 'Prakash Joshi', 'Engineering', date(2022, 6, 15), 'Present', 0, '99000.00'),
]


def demo_rows() -> list[dict]:
    return [{'id': code, 'emp_code': code, 'name_en': name, 'department': dept, 'join_date': joined, 'status': st,
             'late': late, 'salary': sal} for code, name, dept, joined, st, late, sal in PEOPLE]


def demo_context(request) -> dict:
    table = Table(request, demo_rows(), [
        Column('emp_code', 'Code', sortable=True, mono=True),
        Column('name_en', 'Name', sortable=True),
        Column('department', 'Department', sortable=True, hide_below='sm'),
        Column('join_date', 'Joined', format='bs_date', sortable=True),
        Column('status', 'Today', render=lambda r: status_badge_html(r['status'], r['late'])),
        Column('salary', 'Monthly gross', format='npr', sortable=True),
    ], id='demo-people', search=('emp_code', 'name_en', 'department'), default_sort='emp_code', page_size=5,
        export='demo-people', empty_title='No employees yet', empty_hint='Add an employee to see them here.', selectable=True,
        bulk_actions='web/partials/demo_bulk.html')
    today = today_npt()
    months = ['Shrawan', 'Bhadra', 'Ashwin', 'Kartik', 'Mangsir', 'Poush']
    now = datetime.now(timezone.utc)
    return {
        'table': table,
        'stats': [
            {'label': 'Present today', 'value': 128, 'hint': 'of 142 expected', 'tone': 'ok'},
            {'label': 'Late', 'value': 9, 'tone': 'warn', 'hint': 'after the 15 min grace'},
            {'label': 'Absent', 'value': 3, 'tone': 'bad'},
            {'label': 'On leave', 'value': 2, 'url': '#'},
        ],
        'statuses': ['Present', 'Working', 'Late', 'Absent', 'OnLeave', 'HalfDay', 'Holiday', 'WeeklyOff', 'Incomplete', 'WFH',
                     'pending', 'approved', 'rejected', 'cancelled', 'draft', 'computed', 'locked', 'disbursed', 'published'],
        'months': months,
        'column_series': [
            {'key': 'gross', 'label': 'Gross pay', 'values': [4120000, 4180000, 4255000, 4302000, 4290000, 4410000]},
            {'key': 'tds', 'label': 'TDS', 'values': [380000, 391000, 402000, 410000, 405000, 431000]},
        ],
        'chart_legend': [{'label': 'Gross pay', 'color': 'var(--hj-hydro)'}, {'label': 'TDS', 'color': 'var(--hj-warn)'}],
        'chart_table': {'columns': ['Month', 'Gross pay', 'TDS'],
                        'rows': [[m, g, t] for m, g, t in zip(months, [4120000, 4180000, 4255000, 4302000, 4290000, 4410000],
                                                              [380000, 391000, 402000, 410000, 405000, 431000])]},
        'line_series': [
            {'key': 'att', 'label': 'Attendance %', 'values': [94, 95, 92, None, 96, 97]},
            {'key': 'late', 'label': 'Late %', 'values': [6, 5, 8, 7, 4, 3]},
        ],
        'donut_data': [{'label': 'Engineering', 'count': 46}, {'label': 'Sales', 'count': 28}, {'label': 'Operations', 'count': 22},
                       {'label': 'Finance', 'count': 12}, {'label': 'People Ops', 'count': 8}],
        'hbar_data': [{'label': 'Kathmandu HQ', 'value': 84}, {'label': 'Pokhara', 'value': 23}, {'label': 'Biratnagar', 'value': 17},
                      {'label': 'Butwal', 'value': 9}],
        'trend': [{'date': add_days(today, -i), 'value': (None if (i % 7) in (1,) else 88 + (i * 7) % 11)} for i in range(20, -1, -1)],
        'report_columns': [{'key': 'name', 'header': 'Name', 'type': 'text'}, {'key': 'dob', 'header': 'Birthday', 'type': 'date'},
                           {'key': 'ssf', 'header': 'SSF member', 'type': 'bool'}, {'key': 'years', 'header': 'Years', 'type': 'number'}],
        'report_rows': [{'name': 'Sita Sharma', 'dob': '1994-10-28', 'ssf': True, 'years': 5},
                        {'name': 'Ram Thapa', 'dob': '1990-01-05', 'ssf': False, 'years': 7},
                        {'name': 'Gita Gurung', 'dob': None, 'ssf': True, 'years': 3}],
        'holidays': {add_days(today, 3): 'Tihar (Laxmi Puja)', add_days(today, 4): 'Tihar (Govardhan Puja)'},
        'today': today, 'past_instant': now - timedelta(minutes=95), 'sample_date': date(2026, 8, 28),
        'spark': [4, 6, 5, 8, 7, 9, 11],
        'money_values': ['2163.46', '123456789.5', '-40000', None],
    }
