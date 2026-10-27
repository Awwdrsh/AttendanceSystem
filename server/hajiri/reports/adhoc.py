"""
Ad-hoc report builder (port of apps/api/src/modules/reports/adhoc.ts). HR picks a dataset, columns, filters and a
sort. Nothing the user sends ever becomes SQL text: datasets, columns and operators are looked up in the whitelist
below and every value is a bound parameter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from hajiri.core.errors import BadRequestError
from hajiri.core.schema import Arr, Bool, Enum, Num, Obj, Str, Union, parse

from .csv import Column


@dataclass(frozen=True)
class C:
    label: str
    sql: str
    type: str  # 'text' | 'number' | 'date' | 'bool'


@dataclass(frozen=True)
class Dataset:
    label: str
    description: str
    from_: str
    default_columns: tuple[str, ...]
    columns: dict[str, C]


# Joins giving `e` its placement today: branch b, department o, manager m.
PLACED = """employee e
  LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> %(today)s::date
  LEFT JOIN branch b ON b.id = a.branch_id
  LEFT JOIN org_unit o ON o.id = a.org_unit_id
  LEFT JOIN employee m ON m.id = a.manager_id"""

WHO = {
    'emp_code': C('Emp code', 'e.emp_code', 'text'),
    'name': C('Name', 'e.name_en', 'text'),
    'department': C('Department', 'o.name_en', 'text'),
    'branch': C('Branch', 'b.name_en', 'text'),
    'employee_status': C('Employee status', 'e.status', 'text'),
}

ADHOC_DATASETS: dict[str, Dataset] = {
    'employees': Dataset(
        label='Employees', description='One row per employee with job, placement and personal details (no ID or bank numbers).',
    from_=PLACED,
    default_columns=('emp_code', 'name', 'designation', 'department', 'branch', 'join_date'),
    columns={
      **WHO,
      'designation': C('Designation', 'e.designation', 'text'),
      'grade': C('Grade', 'e.grade', 'text'),
      'employment_type': C('Employment type', 'a.employment_type', 'text'),
      'manager': C('Manager', 'm.name_en', 'text'),
      'gender': C('Gender', 'e.gender', 'text'),
      'dob': C('Date of birth', 'e.dob', 'date'),
      'age': C('Age', 'extract(year FROM age(%(today)s::date, e.dob))::int', 'number'),
      'marital_status': C('Marital status', "CASE e.marital_status WHEN 'couple' THEN 'married' ELSE e.marital_status END", 'text'),
      'blood_group': C('Blood group', 'e.blood_group', 'text'),
      'nationality': C('Nationality', 'e.nationality', 'text'),
      'join_date': C('Join date', 'e.join_date', 'date'),
      'service_years': C('Years of service', 'round(((%(today)s::date - e.join_date) / 365.25)::numeric, 1)', 'number'),
      'confirmed_on': C('Confirmed on', 'e.confirmed_on', 'date'),
      'probation_end': C('Probation ends', 'e.probation_end', 'date'),
      'exit_date': C('Exit date', 'e.exit_date', 'date'),
      'work_email': C('Work email', 'e.email', 'text'),
      'work_phone': C('Work phone', 'coalesce(e.work_phone, e.phone)', 'text'),
      'personal_email': C('Personal email', 'e.personal_email', 'text'),
      'province': C('Province', "e.permanent_address->>'province'", 'text'),
      'district': C('District', "e.permanent_address->>'district'", 'text'),
      'regime': C('Retirement fund', 'upper(e.regime)', 'text'),
      'pan_on_file': C('PAN on file', 'e.pan_enc IS NOT NULL', 'bool'),
      'bank_on_file': C('Bank account on file', 'e.bank_account_enc IS NOT NULL', 'bool'),
      'bank_code': C('Bank', 'e.bank_code', 'text'),
      'ot_eligible': C('Overtime eligible', 'e.ot_eligible', 'bool'),
      'skills': C('Skills', "array_to_string(e.skills, ', ')", 'text'),
        },
    ),
    'education': Dataset(
        label='Education', description='One row per qualification.',
    from_=PLACED + ' JOIN employee_education ed ON ed.employee_id = e.id',
    default_columns=('emp_code', 'name', 'level', 'degree', 'institution', 'end_year'),
    columns={
      **WHO,
      'level': C('Level', 'ed.level', 'text'),
      'degree': C('Degree', 'ed.degree', 'text'),
      'field': C('Field', 'ed.field', 'text'),
      'institution': C('Institution', 'ed.institution', 'text'),
      'board': C('Board or university', 'ed.board', 'text'),
      'start_year': C('From year', 'ed.start_year', 'number'),
      'end_year': C('To year', 'ed.end_year', 'number'),
      'result': C('Result', 'ed.result', 'text'),
        },
    ),
    'family': Dataset(
        label='Family and nominees', description='One row per family member.',
    from_=PLACED + ' JOIN employee_family fm ON fm.employee_id = e.id',
    default_columns=('emp_code', 'name', 'member', 'relation', 'nominee', 'share'),
    columns={
      **WHO,
      'member': C('Family member', 'fm.name', 'text'),
      'relation': C('Relation', "replace(fm.relation, '_', ' ')", 'text'),
      'member_dob': C('Member date of birth', 'fm.dob', 'date'),
      'occupation': C('Occupation', 'fm.occupation', 'text'),
      'dependent': C('Dependent', 'fm.is_dependent', 'bool'),
      'nominee': C('Nominee', 'fm.is_nominee', 'bool'),
      'share': C('Nominee share (%)', 'fm.nominee_share', 'number'),
        },
    ),
    'experience': Dataset(
        label='Previous employment', description='One row per earlier job.',
    from_=PLACED + ' JOIN employee_experience ex ON ex.employee_id = e.id',
    default_columns=('emp_code', 'name', 'organisation', 'job_title', 'from_date', 'to_date'),
    columns={
      **WHO,
      'organisation': C('Organisation', 'ex.organisation', 'text'),
      'job_title': C('Designation there', 'ex.designation', 'text'),
      'from_date': C('From', 'ex.from_date', 'date'),
      'to_date': C('To', 'ex.to_date', 'date'),
      'years': C('Years', 'round(((coalesce(ex.to_date, %(today)s::date) - ex.from_date) / 365.25)::numeric, 1)', 'number'),
      'reason_for_leaving': C('Reason for leaving', 'ex.reason_for_leaving', 'text'),
        },
    ),
    'career_events': Dataset(
        label='Career events', description='Hires, promotions, transfers, confirmations and separations.',
    from_=PLACED + " JOIN career_event ce ON ce.employee_id = e.id AND ce.status <> 'cancelled'",
    default_columns=('emp_code', 'name', 'event', 'effective_date', 'new_designation'),
    columns={
      **WHO,
      'event': C('Event', "replace(ce.kind, '_', ' ')", 'text'),
      'effective_date': C('Effective date', 'ce.effective_date', 'date'),
      'event_status': C('Event status', 'ce.status', 'text'),
      'new_designation': C('New designation', "ce.details->>'designation'", 'text'),
      'new_grade': C('New grade', "ce.details->>'grade'", 'text'),
      'separation_type': C('Separation type', "replace(ce.details->>'separationType', '_', ' ')", 'text'),
      'note': C('Note', 'ce.note', 'text'),
        },
    ),
    'leave_balances': Dataset(
        label='Leave balances', description='One row per employee and leave type, with the balance today.',
    from_=PLACED + " JOIN leave_type lt ON lt.company_id = e.company_id\n      LEFT JOIN LATERAL (SELECT coalesce(sum(l.delta_days), 0) AS balance, coalesce(sum(-l.delta_days) FILTER (WHERE l.reason = 'usage'), 0) AS used\n                           FROM leave_ledger l WHERE l.employee_id = e.id AND l.leave_type_id = lt.id AND l.effective_date <= %(today)s::date) lb ON true",
    default_columns=('emp_code', 'name', 'leave_type', 'balance'),
    columns={
      **WHO,
      'leave_type': C('Leave type', 'lt.name_en', 'text'),
      'leave_code': C('Leave code', 'lt.code', 'text'),
      'balance': C('Balance (days)', 'lb.balance', 'number'),
      'used': C('Used (days)', 'lb.used', 'number'),
      'paid': C('Paid leave', 'lt.paid', 'bool'),
        },
    ),
}


OPS = ('eq', 'neq', 'contains', 'starts', 'gt', 'gte', 'lt', 'lte', 'empty', 'not_empty')


def _key(**kw):
    return Str(regex=r'^[a-z_]{1,40}$', **kw)


ADHOC_DEFINITION = Obj({
    'dataset': _key(),
    'columns': Arr(_key(), min=1, max=30),
    'filters': Arr(Obj({'column': _key(), 'op': Enum(OPS),
                        'value': Union(Str(max=200), Num(), Bool(), nullish=True)}, strict=True), max=15, default=list),
    'sort': Obj({'column': _key(), 'dir': Enum(('asc', 'desc'), default='asc')}, strict=True, nullish=True),
    'includeExited': Bool(default=False),
}, strict=True)


def parse_definition(data: Any) -> dict:
    return parse(ADHOC_DEFINITION, data)


def _coerce(type_: str, v: Any, column: str) -> Any:
    if v is None or v == '':
        raise BadRequestError(f'enter a value for the filter on {column}')
    if type_ == 'number':
        try:
            n = float(v) if not isinstance(v, bool) else float(v)
        except (TypeError, ValueError):
            raise BadRequestError(f'{column} needs a number')
        if n != n or n in (float('inf'), float('-inf')):
            raise BadRequestError(f'{column} needs a number')
        return int(n) if n.is_integer() else n
    if type_ == 'date':
        s = str(v)
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', s):
            raise BadRequestError(f'{column} needs a date')
        return s
    if type_ == 'bool':
        if v is True or v in ('true', 'yes'):
            return True
        if v is False or v in ('false', 'no'):
            return False
        raise BadRequestError(f'{column} needs yes or no')
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


@dataclass(frozen=True)
class BuiltQuery:
    sql: str
    params: dict
    count_sql: str
    columns: list[Column]


def build_adhoc_query(d: dict, company_id: str, today: str, limit: int) -> BuiltQuery:
    """Parameterised SQL for a parsed definition: %(company)s and %(today)s, then one parameter per user value."""
    ds = ADHOC_DATASETS.get(d['dataset'])
    if not ds:
        raise BadRequestError('unknown dataset')

    def col(k: str) -> C:
        c = ds.columns.get(k)
        if not c:
            raise BadRequestError(f'unknown column "{k}" for {ds.label}')
        return c

    keys = list(dict.fromkeys(d['columns']))
    select = ', '.join(f'{col(k).sql} AS "{k}"' for k in keys)
    params: dict = {'company': company_id, 'today': today}

    def p(v):
        name = f'p{len(params)}'
        params[name] = v
        return f'%({name})s'

    where = ['e.company_id = %(company)s']
    if not d.get('includeExited'):
        where.append("e.status = 'active'")
    for f in d.get('filters', []):
        c = col(f['column'])
        expr, op_name = c.sql, f['op']
        if op_name == 'empty':
            where.append(f"nullif({expr}::text, '') IS NULL" if c.type == 'text' else f'{expr} IS NULL')
        elif op_name == 'not_empty':
            where.append(f"nullif({expr}::text, '') IS NOT NULL" if c.type == 'text' else f'{expr} IS NOT NULL')
        elif op_name in ('contains', 'starts'):
            s = re.sub(r'[\\%_]', lambda m: '\\' + m[0], str(_coerce('text', f.get('value'), c.label)))
            where.append(f"{expr}::text ILIKE {p(f'%{s}%' if op_name == 'contains' else f'{s}%')}")
        else:
            v = _coerce(c.type, f.get('value'), c.label)
            op = {'eq': '=', 'neq': '<>', 'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<='}[op_name]
            if c.type == 'text' and op_name in ('eq', 'neq'):
                where.append(f'lower({expr}::text) {op} lower({p(v)})')
            elif c.type == 'text':
                where.append(f'{expr}::text {op} {p(v)}')
            elif c.type == 'date':
                where.append(f'{expr} {op} {p(v)}::date')
            elif c.type == 'bool':
                if op_name not in ('eq', 'neq'):
                    raise BadRequestError(f'{c.label} can only be compared with "is" or "is not"')
                where.append(f'{expr} {op} {p(v)}::boolean')
            else:
                where.append(f'{expr} {op} {p(v)}::numeric')
    sort = d.get('sort')
    order = f"{col(sort['column']).sql} {'DESC' if sort.get('dir') == 'desc' else 'ASC'} NULLS LAST, e.emp_code" if sort else 'e.emp_code'
    where_sql = ' AND '.join(where)
    lim = max(1, min(limit, 10_000))
    return BuiltQuery(
        sql=f'SELECT {select} FROM {ds.from_} WHERE {where_sql} ORDER BY {order} LIMIT {lim}', params=params,
        count_sql=f'SELECT count(*)::int AS n FROM {ds.from_} WHERE {where_sql}',
        columns=[Column(k, col(k).label, col(k).type) for k in keys])


def datasets() -> dict:
    """What the builder can offer: datasets with their whitelisted columns, and the operators."""
    return {'datasets': [{'key': k, 'label': d.label, 'description': d.description,
                          'columns': [{'key': ck, 'label': c.label, 'type': c.type} for ck, c in d.columns.items()],
                          'defaultColumns': list(d.default_columns)} for k, d in ADHOC_DATASETS.items()],
            'operators': list(OPS)}
