"""
People record reports (port of reportCatalogRoutes in apps/api/src/modules/reports/catalog.ts). HR runs all of them;
a manager runs the two team reports, limited to their team. Every report reads the ACTIVE company only. Each returns a
Table; `?format=csv` gives Bikram Sambat dates and neutralised formula cells (send_table).
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from django.db.models import BooleanField, Case, Count, DurationField, Exists, ExpressionWrapper, F, Func, IntegerField
from django.db.models import OuterRef, Q, Subquery, TextField, Value, When
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Coalesce, Replace
from django.db.models.lookups import LessThan
from rest_framework import serializers

from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.clock import npt_today
from hajiri.core.collation import collate_key
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError
from hajiri.core.validation import IsoDateField, plain, validate
from hajiri.db.models import (
    Employee, EmployeeAssignment, EmployeeDocument, EmployeeEducation, EmployeeEmergencyContact, EmployeeExit,
    EmployeeFamily, OrgUnit,
)

from .csv import Table, to_fixed, cols

PEOPLE_REPORTS = ('headcount-on-date', 'statistics', 'joiners-leavers', 'birthdays-anniversaries', 'nominees',
                  'emergency-contacts', 'addresses', 'education', 'licence-register', 'record-completeness')
# Reports a manager may run for their own team; the rest hold personal data and are HR only.
MANAGER_REPORTS = frozenset({'headcount-on-date', 'joiners-leavers'})


class Filters(serializers.Serializer):
    format = serializers.ChoiceField(('json', 'csv'), required=False)
    branchId = serializers.UUIDField(required=False)
    orgUnitId = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(('active', 'exited', 'all'), default='active')


def _query(name: str, fields: dict) -> type[serializers.Serializer]:
    return type(name, (serializers.Serializer,), fields)


def _pick(query: dict, *keys: str) -> dict:
    return {k: query[k] for k in keys if k in query}


def _day(v) -> date:
    return date.fromisoformat(v) if isinstance(v, str) else v


def _subtree(company_id: str, unit_id: str) -> set[str]:
    """A department and every unit below it (the org tree of one company is small: walk it in memory)."""
    children: dict[str, list[str]] = {}
    for uid, parent in OrgUnit.objects.for_company(company_id).values_list('id', 'parent_id'):
        if parent:
            children.setdefault(str(parent), []).append(str(uid))
    out, todo = set(), [str(unit_id)]
    while todo:
        u = todo.pop()
        if u not in out:
            out.add(u)
            todo.extend(children.get(u, ()))
    return out


def people_in_scope(user: AuthUser, f: dict, as_of, *, dated: bool = False):
    """Employees of the active company by status, branch and department (with sub-units) on `as_of`; for managers,
    their direct reports and themselves."""
    cid = company_id_of(user)
    qs = Employee.objects.for_company(cid)
    if not dated:
        if f['status'] == 'active':
            qs = qs.filter(status=Employee.Status.ACTIVE)
        if f['status'] == 'exited':
            qs = qs.filter(status=Employee.Status.EXITED)
    placed = EmployeeAssignment.objects.filter(employee=OuterRef('pk'), valid__contains=_day(as_of))
    if f.get('branchId'):
        qs = qs.filter(Exists(placed.filter(branch_id=f['branchId'])))
    if f.get('orgUnitId'):
        qs = qs.filter(Exists(placed.filter(org_unit_id__in=_subtree(cid, f['orgUnitId']))))
    if not has_role(user, 'hr'):
        qs = qs.filter(Q(pk=user.employee_id) | Exists(placed.filter(manager_id=user.employee_id)))
    return qs


def placement(as_of, ref: str = 'pk') -> dict:
    """The placement on a date as annotations: branch, department, manager and employment type."""
    a = EmployeeAssignment.objects.filter(employee=OuterRef(ref), valid__contains=_day(as_of))
    return {'p_branch': Subquery(a.values('branch__name_en')[:1]), 'p_department': Subquery(a.values('org_unit__name_en')[:1]),
            'p_manager': Subquery(a.values('manager__name_en')[:1]), 'p_type': Subquery(a.values('employment_type')[:1]),
            'p_unit_id': Subquery(a.values('org_unit_id')[:1]), 'p_manager_id': Subquery(a.values('manager_id')[:1])}


def _check_access(user: AuthUser, name: str) -> None:
    if not has_role(user, 'hr', 'manager'):
        raise ForbiddenError('requires role hr or manager')
    if name not in PEOPLE_REPORTS:
        raise NotFoundError('not found')
    if not has_role(user, 'hr') and name not in MANAGER_REPORTS:
        raise ForbiddenError('this report is for HR only')


def _address(a) -> str | None:
    """nullif(concat_ws(', ', street, 'Ward ' || ward, municipality, district, province), ''): parts that are set."""
    if not isinstance(a, dict):
        return None
    ward = a.get('ward')
    parts = [a.get('street'), None if ward is None else f'Ward {_json_text(ward)}', a.get('municipality'), a.get('district'),
             a.get('province')]
    return ', '.join(_json_text(p) for p in parts if p is not None) or None


def _json_text(v) -> str:
    """A JSON value as `->>` gives it."""
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _age(on: date, field_name: str):
    return Func(Value(on), F(field_name), function='age', output_field=DurationField())


def _younger(on: date, field_name: str, years: int) -> LessThan:
    """age(on, field) < interval 'N years', with PostgreSQL's interval arithmetic (a month counts as 30 days)."""
    return LessThan(_age(on, field_name), Cast(Value(f'{years} years'), DurationField()))


def _band(on: date, field_name: str, limits: tuple[int, ...]):
    """1..len(limits) for the first limit the age is under, len(limits)+1 above them all, 9 when not recorded."""
    whens = [When(**{f'{field_name}__isnull': True}, then=Value(9))]
    whens += [When(_younger(on, field_name, y), then=Value(i)) for i, y in enumerate(limits, start=1)]
    return Case(*whens, default=Value(len(limits) + 1), output_field=IntegerField())


AGE_BANDS = {1: 'Under 25', 2: '25 to 34', 3: '35 to 44', 4: '45 to 54', 5: '55 and over', 9: 'Not recorded'}
SERVICE_BANDS = {1: 'Under 1 year', 2: '1 to 3 years', 3: '3 to 5 years', 4: '5 to 10 years', 5: '10 years or more',
                 9: 'Not recorded'}
DIMENSIONS = ('Age', 'Length of service', 'Gender', 'Marital status', 'Province (permanent address)',
              'District (permanent address)')
_GENDER_SORT = {'female': 1, 'male': 2, 'other': 3}
_MARITAL = {'single': ('Single', 1), 'couple': ('Married', 2)}


def _same_day(d: date, born: date) -> bool:
    """Day d is the anniversary of `born` (29 February counts on 28 February when d's year is not a multiple of 4)."""
    md = born.strftime('%m-%d')
    if md == '02-29' and d.year % 4 != 0:
        md = '02-28'
    return d.strftime('%m-%d') == md


def _jsonb_key(k: str):
    """The order jsonb keeps object keys in (shorter first, then by bytes), which the completeness list follows."""
    b = k.encode()
    return (len(b), b)


def people_report(user: AuthUser, name: str, query: dict) -> Table:
    _check_access(user, name)
    today = npt_today()
    cid = company_id_of(user)
    f = plain(validate(Filters, _pick(query, 'format', 'branchId', 'orgUnitId', 'status')))

    def base(as_of=today, **kw):
        return people_in_scope(user, f, as_of, **kw).annotate(**placement(as_of))

    if name == 'headcount-on-date':
        q = validate(_query('HeadcountQuery', {'date': IsoDateField(default=today.isoformat())}), _pick(query, 'date'))
        on = date.fromisoformat(q['date'])
        found = (base(on, dated=True)
                 .filter(Q(join_date__isnull=True) | Q(join_date__lte=on), Q(exit_date__isnull=True) | Q(exit_date__gte=on))
                 .annotate(g_branch=Coalesce('p_branch', Value('(no branch)'), output_field=TextField()),
                           g_department=Coalesce('p_department', Value('(no department)'), output_field=TextField()),
                           g_type=Coalesce('p_type', Value(''), output_field=TextField()))
                 .values('g_branch', 'g_department', 'g_type')
                 .annotate(headcount=Count('id'), female=Count('id', filter=Q(gender=Employee.Gender.FEMALE)),
                           male=Count('id', filter=Q(gender=Employee.Gender.MALE)),
                           other=Count('id', filter=Q(gender__isnull=True) | Q(gender=Employee.Gender.OTHER)))
                 .order_by('g_branch', 'g_department', 'g_type'))
        out = [{'branch': x['g_branch'], 'department': x['g_department'], 'employment_type': x['g_type'],
                'headcount': x['headcount'], 'female': x['female'], 'male': x['male'], 'other': x['other']} for x in found]
        return Table(cols(('branch', 'Branch'), ('department', 'Department'), ('employment_type', 'Employment type'),
                          ('headcount', 'Headcount', 'number'), ('female', 'Female', 'number'), ('male', 'Male', 'number'),
                          ('other', 'Other or not recorded', 'number')), out, f"headcount-{q['date']}")

    if name == 'statistics':
        people = (people_in_scope(user, f, today)
                  .annotate(age_band=_band(today, 'dob', (25, 35, 45, 55)),
                            service_band=_band(today, 'join_date', (1, 3, 5, 10)),
                            province=KT('permanent_address__province'), district=KT('permanent_address__district'))
                  .values('age_band', 'service_band', 'gender', 'marital_status', 'province', 'district'))
        groups: dict[tuple[str, str], list[int]] = {}   # (dimension, bucket) -> [sort, count]

        def add(dimension, bucket, sort):
            g = groups.setdefault((dimension, bucket), [sort, 0])
            g[0], g[1] = min(g[0], sort), g[1] + 1
        for e in people:
            add('Age', AGE_BANDS[e['age_band']], e['age_band'])
            add('Length of service', SERVICE_BANDS[e['service_band']], e['service_band'])
            g = e['gender']
            add('Gender', g.capitalize() if g else 'Not recorded', _GENDER_SORT.get(g, 9))
            add('Marital status', *_MARITAL.get(e['marital_status'], ('Not recorded', 9)))
            for dim, v in (('Province (permanent address)', e['province']), ('District (permanent address)', e['district'])):
                add(dim, v if v else 'Not recorded', 1 if v else 9)
        found = sorted(groups.items(), key=lambda kv: (DIMENSIONS.index(kv[0][0]), kv[1][0], -kv[1][1], collate_key(kv[0][1])))
        totals: dict[str, int] = {}
        for (dim, _), (_, n) in found:
            totals[dim] = totals.get(dim, 0) + n
        out = [{'dimension': dim, 'bucket': bucket, 'count': n,
                'share': f"{to_fixed(n / totals[dim] * 100, 1)}%" if totals.get(dim) else ''}
               for (dim, bucket), (_, n) in found]
        return Table(cols(('dimension', 'Measure'), ('bucket', 'Group'), ('count', 'Employees', 'number'), ('share', 'Share')),
                     out, 'people-statistics')

    if name == 'joiners-leavers':
        q = plain(validate(_query('RangeQuery', {'from': IsoDateField(), 'to': IsoDateField()}), _pick(query, 'from', 'to')))
        if q['from'] > q['to']:
            raise BadRequestError('the start date is after the end date')
        everyone = people_in_scope(user, {**f, 'status': 'all'}, today).annotate(**placement(today))
        left_how = (EmployeeExit.objects.filter(employee=OuterRef('pk'),
                                                status__in=(EmployeeExit.Status.ACCEPTED, EmployeeExit.Status.COMPLETED))
                    .order_by('-created_at').values('kind')[:1])
        fields = ('movement', 'when', 'emp_code', 'name', 'designation', 'branch', 'department', 'employment_type', 'exit_kind')

        def moves(label, column, exit_kind):
            return (everyone.filter(**{f'{column}__range': (q['from'], q['to'])})
                    .annotate(movement=Value(label), when=F(column), name=F('name_en'), branch=F('p_branch'),
                              department=F('p_department'), employment_type=F('p_type'), exit_kind=exit_kind)
                    .values(*fields))
        found = (moves('Joined', 'join_date', Value(None, output_field=TextField()))
                 .union(moves('Left', 'exit_date', Subquery(left_how, output_field=TextField())), all=True)
                 .order_by('when', 'emp_code'))
        out = [{'movement': x['movement'], 'date': x['when'], 'emp_code': x['emp_code'], 'name': x['name'],
                'designation': x['designation'], 'branch': x['branch'], 'department': x['department'],
                'employment_type': x['employment_type'],
                'exit_kind': str(x['exit_kind']).replace('_', ' ', 1) if x['exit_kind'] else ''} for x in found]
        return Table(cols(('movement', 'Movement'), ('date', 'Date', 'date'), ('emp_code', 'Emp code'), ('name', 'Name'),
                          ('designation', 'Designation'), ('branch', 'Branch'), ('department', 'Department'),
                          ('employment_type', 'Employment type'), ('exit_kind', 'Reason for leaving')),
                     out, f"joiners-leavers-{q['from']}_{q['to']}")

    if name == 'birthdays-anniversaries':
        q = validate(_query('WindowQuery', {'from': IsoDateField(default=today.isoformat()),
                                            'days': serializers.IntegerField(min_value=1, max_value=92, default=30)}),
                     _pick(query, 'from', 'days'))
        start = date.fromisoformat(q['from'])
        window = [start + timedelta(days=i) for i in range(q['days'])]
        people = (people_in_scope(user, {**f, 'status': 'active'}, today).annotate(**placement(today))
                  .values('emp_code', 'name_en', 'dob', 'join_date', 'p_department', 'p_branch'))
        found = []
        for e in people:
            row = {'emp_code': e['emp_code'], 'name': e['name_en'], 'department': e['p_department'], 'branch': e['p_branch']}
            for d in window:
                if e['dob'] and _same_day(d, e['dob']):
                    found.append({'occasion': 'Birthday', 'date': d, **row, 'years': d.year - e['dob'].year})
                if e['join_date'] and e['join_date'] < d and _same_day(d, e['join_date']):
                    found.append({'occasion': 'Work anniversary', 'date': d, **row, 'years': d.year - e['join_date'].year})
        found.sort(key=lambda r: (r['date'], r['occasion'], collate_key(r['name'])))
        return Table(cols(('date', 'Date', 'date'), ('occasion', 'Occasion'), ('emp_code', 'Emp code'), ('name', 'Name'),
                          ('department', 'Department'), ('branch', 'Branch'), ('years', 'Years', 'number')),
                     found, f"birthdays-anniversaries-{q['from']}")

    def of_people(model, **filters):
        """Rows of an employee's records (family, contacts, education) for the people in scope, with their placement."""
        return (model.objects.for_company(cid).filter(employee__in=people_in_scope(user, f, today), **filters)
                .annotate(**placement(today, 'employee_id'), e_code=F('employee__emp_code'), e_name=F('employee__name_en')))

    if name == 'nominees':
        found = (of_people(EmployeeFamily, is_nominee=True)
                 .annotate(n_relation=Replace('relation', Value('_'), Value(' '), output_field=TextField()), n_share=Cast('nominee_share', TextField()))
                 .order_by('employee__emp_code', 'sort', 'name')
                 .values('e_code', 'e_name', 'p_department', 'name', 'n_relation', 'dob', 'n_share', 'phone'))
        out = [{'emp_code': x['e_code'], 'employee': x['e_name'], 'department': x['p_department'], 'nominee': x['name'],
                'relation': x['n_relation'], 'dob': x['dob'], 'share': x['n_share'], 'phone': x['phone']} for x in found]
        return Table(cols(('emp_code', 'Emp code'), ('employee', 'Employee'), ('department', 'Department'), ('nominee', 'Nominee'),
                          ('relation', 'Relation'), ('dob', 'Date of birth', 'date'), ('share', 'Share (%)'), ('phone', 'Phone')),
                     out, 'nominees')

    if name == 'emergency-contacts':
        found = (of_people(EmployeeEmergencyContact).annotate(blood=F('employee__blood_group'))
                 .order_by('employee__emp_code', 'sort', 'name')
                 .values('e_code', 'e_name', 'p_department', 'blood', 'name', 'relation', 'phone', 'alt_phone', 'address'))
        out = [{'emp_code': x['e_code'], 'employee': x['e_name'], 'department': x['p_department'], 'blood_group': x['blood'],
                'contact': x['name'], 'relation': x['relation'], 'phone': x['phone'], 'alt_phone': x['alt_phone'],
                'address': x['address']} for x in found]
        return Table(cols(('emp_code', 'Emp code'), ('employee', 'Employee'), ('department', 'Department'),
                          ('blood_group', 'Blood group'), ('contact', 'Contact'), ('relation', 'Relation'), ('phone', 'Phone'),
                          ('alt_phone', 'Other phone'), ('address', 'Address')), out, 'emergency-contacts')

    if name == 'addresses':
        found = (base().annotate(province=KT('permanent_address__province'), district=KT('permanent_address__district'))
                 .order_by('emp_code')
                 .values('emp_code', 'name_en', 'p_department', 'p_branch', 'province', 'district', 'permanent_address',
                         'temporary_address'))
        out = [{'emp_code': x['emp_code'], 'employee': x['name_en'], 'department': x['p_department'], 'branch': x['p_branch'],
                'province': x['province'], 'district': x['district'], 'permanent_address': _address(x['permanent_address']),
                'temporary_address': _address(x['temporary_address'])} for x in found]
        return Table(cols(('emp_code', 'Emp code'), ('employee', 'Employee'), ('department', 'Department'), ('branch', 'Branch'),
                          ('province', 'Province'), ('district', 'District'), ('permanent_address', 'Permanent address'),
                          ('temporary_address', 'Temporary address')), out, 'addresses')

    if name == 'education':
        found = (of_people(EmployeeEducation)
                 .order_by('employee__emp_code', F('end_year').desc(nulls_last=True), 'sort')
                 .values('e_code', 'e_name', 'p_department', 'level', 'degree', 'field', 'institution', 'board', 'start_year',
                         'end_year', 'result'))
        level = {'see': 'SEE', 'plus_two': '+2', 'diploma': 'Diploma', 'bachelor': 'Bachelor', 'master': 'Master',
                 'mphil': 'MPhil', 'phd': 'PhD', 'other': 'Other'}
        out = [{'emp_code': x['e_code'], 'employee': x['e_name'], 'department': x['p_department'],
                'level': level.get(str(x['level']), x['level']), **{k: x[k] for k in ('degree', 'field', 'institution', 'board',
                                                                                       'start_year', 'end_year', 'result')}}
               for x in found]
        return Table(cols(('emp_code', 'Emp code'), ('employee', 'Employee'), ('department', 'Department'), ('level', 'Level'),
                          ('degree', 'Degree'), ('field', 'Field'), ('institution', 'Institution'),
                          ('board', 'Board or university'), ('start_year', 'From', 'number'), ('end_year', 'To', 'number'),
                          ('result', 'Result')), out, 'education')

    if name == 'licence-register':
        q = validate(_query('LicenceQuery', {'withinDays': serializers.IntegerField(min_value=0, max_value=3650, default=90)}),
                     _pick(query, 'withinDays'))
        on_file = lambda col: ExpressionWrapper(Q(**{f'{col}__isnull': False}), output_field=BooleanField())  # noqa: E731
        people = (base().annotate(has_passport=on_file('passport_no_enc'), has_licence=on_file('licence_no_enc'),
                                  has_citizenship=on_file('citizenship_no_enc'))
                  .values('emp_code', 'name_en', 'p_department', 'has_passport', 'passport_expiry', 'has_licence',
                          'licence_expiry', 'has_citizenship', 'citizenship_district', 'citizenship_issued_on'))
        # ID numbers are encrypted; the register shows only whether one is on file, never the number.
        found = []
        for e in people:
            who = {'emp_code': e['emp_code'], 'employee': e['name_en'], 'department': e['p_department']}
            if e['has_passport'] or e['passport_expiry']:
                found.append({**who, 'document': 'Passport', 'on_file': e['has_passport'], 'expiry': e['passport_expiry'],
                              'detail': None})
            if e['has_licence'] or e['licence_expiry']:
                found.append({**who, 'document': 'Driving licence', 'on_file': e['has_licence'], 'expiry': e['licence_expiry'],
                              'detail': None})
            if e['has_citizenship']:
                parts = [e['citizenship_district'], 'issued' if e['citizenship_issued_on'] else None]
                found.append({**who, 'document': 'Citizenship', 'on_file': True, 'expiry': None,
                              'detail': ', '.join(p for p in parts if p is not None) or None})
        state_word = {EmployeeDocument.Status.VERIFIED: 'Verified', EmployeeDocument.Status.PENDING: 'Waiting for HR'}
        for d in (EmployeeDocument.objects.for_company(cid)
                  .filter(employee__in=people_in_scope(user, f, today), is_current=True, deleted_at__isnull=True,
                          expiry_date__isnull=False)
                  .annotate(**placement(today, 'employee_id'))
                  .values('employee__emp_code', 'employee__name_en', 'p_department', 'category__name', 'title', 'expiry_date',
                          'status')):
            found.append({'emp_code': d['employee__emp_code'], 'employee': d['employee__name_en'],
                          'department': d['p_department'], 'document': f"{d['category__name']}: {d['title']}",
                          'on_file': True, 'expiry': d['expiry_date'], 'detail': state_word.get(d['status'], 'Rejected')})
        found.sort(key=lambda r: (r['expiry'] is None, r['expiry'] or date.min, collate_key(r['emp_code'])))
        limit = today + timedelta(days=q['withinDays'])

        def state(exp):
            if not exp:
                return 'No expiry'
            return 'Expired' if exp < today else 'Expiring soon' if exp <= limit else 'Valid'
        out = [{**x, 'on_file': 'Recorded' if x['on_file'] else 'Not recorded', 'state': state(x['expiry'])} for x in found]
        return Table(cols(('emp_code', 'Emp code'), ('employee', 'Employee'), ('department', 'Department'),
                          ('document', 'Document'), ('on_file', 'Number'), ('detail', 'Detail'), ('expiry', 'Expires', 'date'),
                          ('state', 'State')), out, 'licence-register')

    # record-completeness
    has = lambda model: Exists(model.objects.filter(employee=OuterRef('pk')))  # noqa: E731
    is_set = lambda col: ExpressionWrapper(Q(**{f'{col}__isnull': False}), output_field=BooleanField())  # noqa: E731
    people = (base().annotate(c_citizenship=is_set('citizenship_no_enc'), c_pan=is_set('pan_enc'),
                              c_bank=is_set('bank_account_enc'), district=KT('permanent_address__district'),
                              c_contact=has(EmployeeEmergencyContact), c_family=has(EmployeeFamily),
                              c_education=has(EmployeeEducation))
              .order_by('emp_code')
              .values('emp_code', 'name_en', 'p_department', 'dob', 'gender', 'marital_status', 'phone', 'personal_phone',
                      'personal_email', 'district', 'c_citizenship', 'c_pan', 'c_bank', 'regime', 'ssf_id', 'designation',
                      'p_unit_id', 'p_manager_id', 'c_contact', 'c_family', 'c_education', 'avatar_file_id'))
    out = []
    for x in people:
        checks = {
            'Date of birth': x['dob'] is not None, 'Gender': x['gender'] is not None,
            'Marital status': x['marital_status'] is not None,
            'Phone': (x['phone'] if x['phone'] is not None else x['personal_phone']) is not None,
            'Personal email': x['personal_email'] is not None, 'Permanent address': bool(x['district']),
            'Citizenship': x['c_citizenship'], 'PAN': x['c_pan'], 'Bank account': x['c_bank'],
            'SSF ID': x['regime'] != Employee.Regime.SSF or bool(x['ssf_id']), 'Designation': bool(x['designation']),
            'Department': x['p_unit_id'] is not None, 'Manager': x['p_manager_id'] is not None,
            'Emergency contact': x['c_contact'], 'Family': x['c_family'], 'Education': x['c_education'],
            'Photo': x['avatar_file_id'] is not None,
        }
        keys = sorted(checks, key=_jsonb_key)
        done = sum(1 for v in checks.values() if v)
        out.append({'emp_code': x['emp_code'], 'employee': x['name_en'], 'department': x['p_department'],
                    'complete': math.floor(done / len(checks) * 100 + 0.5),
                    'missing': ', '.join(k for k in keys if not checks[k])})
    out.sort(key=lambda r: (r['complete'], str(r['emp_code'])))
    return Table(cols(('emp_code', 'Emp code'), ('employee', 'Employee'), ('department', 'Department'),
                      ('complete', 'Complete (%)', 'number'), ('missing', 'Missing')), out, 'record-completeness')
