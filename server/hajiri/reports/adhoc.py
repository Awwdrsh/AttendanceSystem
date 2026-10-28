"""
Ad-hoc report builder (port of apps/api/src/modules/reports/adhoc.ts). HR picks a dataset, columns, filters and a
sort. Nothing the user sends ever becomes SQL: datasets, columns and operators are looked up in the whitelist below,
each column is an ORM expression, and every value is a bound parameter. A query reads only the active company.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Callable

from django.db import models
from django.db.models import BooleanField, Case, DecimalField, ExpressionWrapper, F, Func, IntegerField, OuterRef, Q
from django.db.models import Subquery, Sum, TextField, Value, When
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Coalesce, Lower, Replace, Upper
from django.db.models.lookups import Exact, GreaterThan, GreaterThanOrEqual, LessThan, LessThanOrEqual
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from hajiri.company_settings.fields import JsonBool, JsonString, StrictSerializer, plain_dict  # noqa: F401 - re-exported
from hajiri.core.errors import BadRequestError
from hajiri.core.validation import validate
from hajiri.db.models import (
    CareerEvent, Employee, EmployeeAssignment, EmployeeEducation, EmployeeExperience, EmployeeFamily, LeaveLedger,
)

from .csv import Column

Expr = Callable[['Ctx'], Any]


@dataclass(frozen=True)
class Ctx:
    """What a column expression needs: the path from the dataset's rows to the employee ('' or 'employee__') and today
    (Nepal date)."""
    e: str
    today: date

    def f(self, name: str) -> F:
        return F(self.e + name)

    @property
    def employee_ref(self) -> OuterRef:
        return OuterRef(self.e + 'id' if self.e else 'pk')

    def today_value(self) -> Value:
        return Value(self.today, output_field=models.DateField())


@dataclass(frozen=True)
class C:
    label: str
    expr: Expr
    type: str  # 'text' | 'number' | 'date' | 'bool'


@dataclass(frozen=True)
class Dataset:
    label: str
    description: str
    rows: Callable[[str], models.QuerySet]   # company id -> the dataset's rows
    employee: str                             # path from a row to its employee
    default_columns: tuple[str, ...]
    columns: dict[str, C]


def field(name: str) -> Expr:
    return lambda c: F(name)


def emp(name: str) -> Expr:
    return lambda c: c.f(name)


def placed(path: str) -> Expr:
    """The employee's placement today (branch, department, manager, employment type)."""
    return lambda c: Subquery(EmployeeAssignment.objects.filter(employee=c.employee_ref, valid__contains=c.today)
                              .values(path)[:1])


def is_set(name: str) -> Expr:
    return lambda c: ExpressionWrapper(Q(**{c.e + name + '__isnull': False}), output_field=BooleanField())


def underscores_to_spaces(expr: Expr) -> Expr:
    return lambda c: Replace(expr(c), Value('_'), Value(' '), output_field=TextField())


def years_between(start: Expr, end: Expr | None = None) -> Expr:
    """Years from start to end (today when None) to one decimal: round(((end - start) / 365.25)::numeric, 1)."""
    def build(c):
        stop = end(c) if end else c.today_value()
        return Func(stop, start(c), template='round(((%(expressions)s) / 365.25)::numeric, 1)', arg_joiner=' - ',
                    output_field=DecimalField())
    return build


def age_today(c: Ctx):
    return Cast(Func(Func(c.today_value(), c.f('dob'), function='age'), template='extract(year FROM %(expressions)s)'),
                IntegerField())


WHO = {
    'emp_code': C('Emp code', emp('emp_code'), 'text'),
    'name': C('Name', emp('name_en'), 'text'),
    'department': C('Department', placed('org_unit__name_en'), 'text'),
    'branch': C('Branch', placed('branch__name_en'), 'text'),
    'employee_status': C('Employee status', emp('status'), 'text'),
}


def _ledger(c: Ctx, **filters):
    rows = LeaveLedger.objects.filter(employee=OuterRef('pk'), leave_type=OuterRef('_leave_type'),
                                      effective_date__lte=c.today, **filters)
    return rows.values('employee').annotate(n=Sum('delta_days')).values('n')[:1]


def _balance(c: Ctx):
    return Coalesce(Subquery(_ledger(c), output_field=DecimalField()), Value(Decimal(0)), output_field=DecimalField())


def _used(c: Ctx):
    return -Coalesce(Subquery(_ledger(c, reason='usage'), output_field=DecimalField()), Value(Decimal(0)),
                     output_field=DecimalField())


ADHOC_DATASETS: dict[str, Dataset] = {
    'employees': Dataset(
        label='Employees', description='One row per employee with job, placement and personal details (no ID or bank numbers).',
        rows=lambda cid: Employee.objects.for_company(cid), employee='',
        default_columns=('emp_code', 'name', 'designation', 'department', 'branch', 'join_date'),
        columns={
            **WHO,
            'designation': C('Designation', field('designation'), 'text'),
            'grade': C('Grade', field('grade'), 'text'),
            'employment_type': C('Employment type', placed('employment_type'), 'text'),
            'manager': C('Manager', placed('manager__name_en'), 'text'),
            'gender': C('Gender', field('gender'), 'text'),
            'dob': C('Date of birth', field('dob'), 'date'),
            'age': C('Age', age_today, 'number'),
            'marital_status': C('Marital status', lambda c: Case(When(marital_status='couple', then=Value('married')),
                                                                 default=F('marital_status'), output_field=TextField()), 'text'),
            'blood_group': C('Blood group', field('blood_group'), 'text'),
            'nationality': C('Nationality', field('nationality'), 'text'),
            'join_date': C('Join date', field('join_date'), 'date'),
            'service_years': C('Years of service', years_between(field('join_date')), 'number'),
            'confirmed_on': C('Confirmed on', field('confirmed_on'), 'date'),
            'probation_end': C('Probation ends', field('probation_end'), 'date'),
            'exit_date': C('Exit date', field('exit_date'), 'date'),
            'work_email': C('Work email', field('email'), 'text'),
            'work_phone': C('Work phone', lambda c: Coalesce('work_phone', 'phone'), 'text'),
            'personal_email': C('Personal email', field('personal_email'), 'text'),
            'province': C('Province', lambda c: KT('permanent_address__province'), 'text'),
            'district': C('District', lambda c: KT('permanent_address__district'), 'text'),
            'regime': C('Retirement fund', lambda c: Upper('regime'), 'text'),
            'pan_on_file': C('PAN on file', is_set('pan_enc'), 'bool'),
            'bank_on_file': C('Bank account on file', is_set('bank_account_enc'), 'bool'),
            'bank_code': C('Bank', field('bank_code'), 'text'),
            'ot_eligible': C('Overtime eligible', field('ot_eligible'), 'bool'),
            'skills': C('Skills', lambda c: Func(F('skills'), Value(', '), function='array_to_string',
                                                 output_field=TextField()), 'text'),
        },
    ),
    'education': Dataset(
        label='Education', description='One row per qualification.',
        rows=lambda cid: EmployeeEducation.objects.for_company(cid), employee='employee__',
        default_columns=('emp_code', 'name', 'level', 'degree', 'institution', 'end_year'),
        columns={
            **WHO,
            'level': C('Level', field('level'), 'text'),
            'degree': C('Degree', field('degree'), 'text'),
            'field': C('Field', field('field'), 'text'),
            'institution': C('Institution', field('institution'), 'text'),
            'board': C('Board or university', field('board'), 'text'),
            'start_year': C('From year', field('start_year'), 'number'),
            'end_year': C('To year', field('end_year'), 'number'),
            'result': C('Result', field('result'), 'text'),
        },
    ),
    'family': Dataset(
        label='Family and nominees', description='One row per family member.',
        rows=lambda cid: EmployeeFamily.objects.for_company(cid), employee='employee__',
        default_columns=('emp_code', 'name', 'member', 'relation', 'nominee', 'share'),
        columns={
            **WHO,
            'member': C('Family member', field('name'), 'text'),
            'relation': C('Relation', underscores_to_spaces(field('relation')), 'text'),
            'member_dob': C('Member date of birth', field('dob'), 'date'),
            'occupation': C('Occupation', field('occupation'), 'text'),
            'dependent': C('Dependent', field('is_dependent'), 'bool'),
            'nominee': C('Nominee', field('is_nominee'), 'bool'),
            'share': C('Nominee share (%)', field('nominee_share'), 'number'),
        },
    ),
    'experience': Dataset(
        label='Previous employment', description='One row per earlier job.',
        rows=lambda cid: EmployeeExperience.objects.for_company(cid), employee='employee__',
        default_columns=('emp_code', 'name', 'organisation', 'job_title', 'from_date', 'to_date'),
        columns={
            **WHO,
            'organisation': C('Organisation', field('organisation'), 'text'),
            'job_title': C('Designation there', field('designation'), 'text'),
            'from_date': C('From', field('from_date'), 'date'),
            'to_date': C('To', field('to_date'), 'date'),
            'years': C('Years', years_between(field('from_date'),
                                              lambda c: Coalesce(F('to_date'), c.today_value())), 'number'),
            'reason_for_leaving': C('Reason for leaving', field('reason_for_leaving'), 'text'),
        },
    ),
    'career_events': Dataset(
        label='Career events', description='Hires, promotions, transfers, confirmations and separations.',
        rows=lambda cid: CareerEvent.objects.for_company(cid).exclude(status=CareerEvent.Status.CANCELLED),
        employee='employee__',
        default_columns=('emp_code', 'name', 'event', 'effective_date', 'new_designation'),
        columns={
            **WHO,
            'event': C('Event', underscores_to_spaces(field('kind')), 'text'),
            'effective_date': C('Effective date', field('effective_date'), 'date'),
            'event_status': C('Event status', field('status'), 'text'),
            'new_designation': C('New designation', lambda c: KT('details__designation'), 'text'),
            'new_grade': C('New grade', lambda c: KT('details__grade'), 'text'),
            'separation_type': C('Separation type', underscores_to_spaces(lambda c: KT('details__separationType')), 'text'),
            'note': C('Note', field('note'), 'text'),
        },
    ),
    'leave_balances': Dataset(
        label='Leave balances', description='One row per employee and leave type, with the balance today.',
        # One row per employee and each leave type of their company (the join through company).
        rows=lambda cid: Employee.objects.for_company(cid).annotate(_leave_type=F('company__leave_types__id'))
        .filter(_leave_type__isnull=False),
        employee='',
        default_columns=('emp_code', 'name', 'leave_type', 'balance'),
        columns={
            **WHO,
            'leave_type': C('Leave type', field('company__leave_types__name_en'), 'text'),
            'leave_code': C('Leave code', field('company__leave_types__code'), 'text'),
            'balance': C('Balance (days)', _balance, 'number'),
            'used': C('Used (days)', _used, 'number'),
            'paid': C('Paid leave', field('company__leave_types__paid'), 'bool'),
        },
    ),
}


OPS = ('eq', 'neq', 'contains', 'starts', 'gt', 'gte', 'lt', 'lte', 'empty', 'not_empty')
KEY = r'^[a-z_]{1,40}$'


def _key(**kw) -> JsonString:
    return JsonString(pattern=KEY, **kw)


@extend_schema_field(OpenApiTypes.ANY)
class FilterValue(serializers.Field):
    """A filter value: a string of at most 200 characters, a number or a boolean (or nothing)."""

    def to_internal_value(self, v):
        if isinstance(v, str):
            if len(v) > 200:
                raise serializers.ValidationError('Invalid input')
            return v
        if isinstance(v, (bool, int, float)):
            return v
        raise serializers.ValidationError('Invalid input')

    def to_representation(self, value):
        return value


class FilterIn(StrictSerializer):
    column = _key()
    op = serializers.ChoiceField(OPS)
    value = FilterValue(required=False, allow_null=True)


class SortIn(StrictSerializer):
    column = _key()
    dir = serializers.ChoiceField(('asc', 'desc'), default='asc')


class AdhocDefinitionIn(StrictSerializer):
    dataset = _key()
    columns = serializers.ListField(child=_key(), min_length=1, max_length=30)
    filters = serializers.ListField(child=FilterIn(), max_length=15, default=list)
    sort = SortIn(required=False, allow_null=True)
    includeExited = JsonBool(default=False)


def parse_definition(data: Any) -> dict:
    return plain_dict(validate(AdhocDefinitionIn, data))


def _coerce(type_: str, v: Any, column: str) -> Any:
    if v is None or v == '':
        raise BadRequestError(f'enter a value for the filter on {column}')
    if type_ == 'number':
        try:
            n = float(v)
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
    rows: models.QuerySet        # limited, ordered, values() of the chosen columns (as `c_<key>`)
    count: models.QuerySet       # the same rows without limit, for the total
    keys: list[str]
    columns: list[Column]

    def fetch(self) -> list[dict]:
        return [{k: r[f'c_{k}'] for k in self.keys} for r in self.rows]


_COMPARE = {'eq': Exact, 'gt': GreaterThan, 'gte': GreaterThanOrEqual, 'lt': LessThan, 'lte': LessThanOrEqual}


def _as_text(expr):
    return Cast(expr, TextField())


def build_adhoc_query(d: dict, company_id: str, today: str, limit: int) -> BuiltQuery:
    """The queryset for a parsed definition, scoped to the company: the chosen columns, the filters (bound values) and
    the sort, at most `limit` rows."""
    ds = ADHOC_DATASETS.get(d['dataset'])
    if not ds:
        raise BadRequestError('unknown dataset')
    ctx = Ctx(ds.employee, date.fromisoformat(today))

    def col(k: str) -> C:
        c = ds.columns.get(k)
        if not c:
            raise BadRequestError(f'unknown column "{k}" for {ds.label}')
        return c

    keys = list(dict.fromkeys(d['columns']))
    used = list(dict.fromkeys(keys + [f['column'] for f in d.get('filters', [])]
                              + ([d['sort']['column']] if d.get('sort') else [])))
    qs = ds.rows(company_id).annotate(**{f'c_{k}': col(k).expr(ctx) for k in used})
    if not d.get('includeExited'):
        qs = qs.filter(**{ds.employee + 'status': Employee.Status.ACTIVE})
    for f in d.get('filters', []):
        c = col(f['column'])
        alias, op = f'c_{f["column"]}', f['op']
        ref = F(alias)
        if op in ('empty', 'not_empty'):
            blank = (Q(**{f'{alias}__isnull': True}) | Q(Exact(_as_text(ref), Value(''))) if c.type == 'text'
                     else Q(**{f'{alias}__isnull': True}))
            qs = qs.filter(blank if op == 'empty' else ~blank)
        elif op in ('contains', 'starts'):
            s = str(_coerce('text', f.get('value'), c.label))
            text = f'{alias}_text'
            qs = qs.alias(**{text: _as_text(ref)}).filter(**{f'{text}__{"icontains" if op == "contains" else "istartswith"}': s})
        else:
            v = _coerce(c.type, f.get('value'), c.label)
            lookup = _COMPARE.get('eq' if op == 'neq' else op)
            if c.type == 'text' and op in ('eq', 'neq'):
                cond = Exact(Lower(_as_text(ref)), Lower(Value(v)))
            elif c.type == 'text':
                cond = lookup(_as_text(ref), Value(v))
            elif c.type == 'date':
                cond = lookup(ref, Value(date.fromisoformat(v), output_field=models.DateField()))
            elif c.type == 'bool':
                if op not in ('eq', 'neq'):
                    raise BadRequestError(f'{c.label} can only be compared with "is" or "is not"')
                cond = Exact(ref, Value(v, output_field=BooleanField()))
            else:
                cond = lookup(ref, Value(Decimal(str(v)), output_field=DecimalField()))
            # `<>` is NOT (=): a row whose value is empty matches neither, as in SQL.
            qs = qs.filter(~Q(cond) if op == 'neq' else cond)
    emp_code = F(ds.employee + 'emp_code').asc()
    sort = d.get('sort')
    if sort:
        by = F(f'c_{sort["column"]}')
        qs = qs.order_by(by.desc(nulls_last=True) if sort.get('dir') == 'desc' else by.asc(nulls_last=True), emp_code)
    else:
        qs = qs.order_by(emp_code)
    lim = max(1, min(limit, 10_000))
    return BuiltQuery(rows=qs.values(*(f'c_{k}' for k in keys))[:lim], count=qs.order_by(), keys=keys,
                      columns=[Column(k, col(k).label, col(k).type) for k in keys])


def datasets() -> dict:
    """What the builder can offer: datasets with their whitelisted columns, and the operators."""
    return {'datasets': [{'key': k, 'label': d.label, 'description': d.description,
                          'columns': [{'key': ck, 'label': c.label, 'type': c.type} for ck, c in d.columns.items()],
                          'defaultColumns': list(d.default_columns)} for k, d in ADHOC_DATASETS.items()],
            'operators': list(OPS)}
