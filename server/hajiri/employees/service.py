"""
The employee master (port of apps/api/src/modules/employees/routes.ts): list, read, sensitive fields, create, edit with
effective-dated assignments and field history, CSV import, assignment history and the employee-visible audit trail.
Everything is scoped to the ACTIVE company; an employee of another company is not found.
"""
from __future__ import annotations

import csv
import io
from datetime import date

from django.db import IntegrityError, transaction
from django.db.backends.postgresql.psycopg_any import DateRange
from django.db.models import BooleanField, ExpressionWrapper, F, FilteredRelation, OuterRef, Q, Subquery
from django.db.models.functions import Now

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, assert_manages, company_id_of, has_role, manages
from hajiri.core.clock import npt_today
from hajiri.core.crypto import field_cipher
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (AppUser, AuditLog, Branch, Designation, Employee, EmployeeAssignment, OrgUnit, Shift,
                              ShiftAssignment)

from .filters import EmployeeFilter
from .serializers import EMPLOYMENT_TYPES, EmployeeIn, EmployeeListQuery  # noqa: F401

PAN_RE = r'^\d{9}$'
Status = Employee.Status

PUBLIC_FIELDS = ('id', 'company_id', 'emp_code', 'name_en', 'name_ne', 'gender', 'dob', 'marital_status', 'join_date', 'exit_date',
                 'designation', 'grade', 'phone', 'email', 'ot_eligible', 'work_from_home', 'weekly_off_days', 'status', 'regime',
                 'ssf_id', 'pf_id', 'cit_id', 'bank_code', 'bank_branch', 'bank_account_last4', 'is_resident', 'remote_area_class',
                 'disability', 'festival_month', 'probation_end', 'created_at', 'has_pan', 'branch_id', 'org_unit_id',
                 'manager_id', 'employment_type', 'shift_id')
_IDS = ('id', 'company_id', 'branch_id', 'org_unit_id', 'manager_id', 'shift_id')

# Specialist admins need the employee list and cards (not PAN, bank or edits) to do payroll and attendance work.
READ_ALL = ('payroll_admin', 'attendance_admin')


def _row(r: dict | None) -> dict | None:
    if r is None:
        return None
    for k in _IDS:
        if r.get(k) is not None:
            r[k] = str(r[k])
    r['weekly_off_days'] = list(r.get('weekly_off_days') or [])
    return r


def public_rows(qs=None, today: date | None = None):
    """Employees as the TS route returns them (PUBLIC_COLS): the record, whether a PAN is on file, today's assignment
    (a LEFT JOIN on the period covering today) and today's shift."""
    today = today or npt_today()
    qs = Employee.objects.all() if qs is None else qs
    shift = ShiftAssignment.objects.filter(employee=OuterRef('pk'), valid__contains=today).values('shift_id')[:1]
    return (qs.annotate(a=FilteredRelation('employee_assignments', condition=Q(employee_assignments__valid__contains=today)))
            .annotate(has_pan=ExpressionWrapper(Q(pan_enc__isnull=False), output_field=BooleanField()),
                      branch_id=F('a__branch_id'), org_unit_id=F('a__org_unit_id'), manager_id=F('a__manager_id'),
                      employment_type=F('a__employment_type'), shift_id=Subquery(shift))
            .values(*PUBLIC_FIELDS))


def _d(s) -> date | None:
    return date.fromisoformat(s) if isinstance(s, str) else s


_REFS = ((Shift, 'shiftId', 'shift'), (Branch, 'branchId', 'branch'), (OrgUnit, 'orgUnitId', 'org unit'), (Employee, 'managerId', 'manager'))


def validate_refs(company_id: str, b: dict, self_id: str | None = None) -> None:
    """Referenced records (shift, branch, org unit, manager) must belong to the caller's company; dates must be sane."""
    for model, key, label in _REFS:
        if b.get(key) and not model.objects.for_company(company_id).filter(pk=b[key]).exists():
            raise BadRequestError(f'{label} does not exist in your company')
    if b.get('managerId') and self_id and b['managerId'] == self_id:
        raise BadRequestError('an employee cannot be their own manager')
    if b.get('joinDate') and b.get('exitDate') and b['exitDate'] < b['joinDate']:
        raise BadRequestError('exit date cannot be before join date')
    if b.get('probationEnd') and b.get('joinDate') and b['probationEnd'] < b['joinDate']:
        raise BadRequestError('probation end cannot be before join date')


def link_designation(employee_id: str) -> None:
    """Keep designation_id in step with the free-text designation: link it to the catalogue entry of the same name, if any."""
    match = (Designation.objects.filter(company=OuterRef('company'), name_en__iexact=OuterRef('designation'))
             .order_by('-active').values('id')[:1])
    Employee.objects.filter(pk=employee_id).update(designation_ref=Subquery(match))


def pan_owner(company_id: str, pan: str, except_id: str | None = None) -> str | None:
    """emp_code of the employee already holding this PAN in the company (other than except_id), via the blind index."""
    qs = Employee.objects.for_company(company_id).filter(pan_bidx=field_cipher().blind_index(pan))
    if except_id:
        qs = qs.exclude(pk=except_id)
    return qs.values_list('emp_code', flat=True).first()


def assert_pan_free(company_id: str, pan: str, except_id: str | None = None) -> None:
    owner = pan_owner(company_id, pan, except_id)
    if owner:
        raise ConflictError(f'this PAN is already recorded for employee {owner}')


def _list_query(query: dict | None) -> dict:
    return plain(validate(EmployeeListQuery, {k: v for k, v in (query or {}).items() if v not in (None, '')}))


def employees_queryset(user: AuthUser, query: dict | None = None):
    """The employee list as a queryset (values rows in the PUBLIC_COLS shape, ordered by code): HR and the payroll and
    attendance admins see the whole active company; a manager the people they manage and themselves."""
    if not has_role(user, 'hr', 'manager', *READ_ALL):
        raise ForbiddenError(f"requires role {' or '.join(('hr', 'manager', *READ_ALL))}")
    q = _list_query(query)
    if q.get('pan') and not has_role(user, 'hr'):
        raise ForbiddenError('PAN lookup requires HR')
    qs = Employee.objects.for_company(company_id_of(user))
    if not has_role(user, 'hr', *READ_ALL):
        qs = qs.filter(Q(pk=user.employee_id) | manages(user.employee_id)) if user.employee_id else qs.none()
    return EmployeeFilter(q, queryset=public_rows(qs)).qs.order_by('emp_code')


def list_employees(user: AuthUser, query: dict | None = None) -> list[dict]:
    """HR (and the payroll and attendance admins, read-only) list the whole company; a manager lists the people they
    manage and themselves."""
    qs = employees_queryset(user, query)
    out = [_row(r) for r in qs]
    if (query or {}).get('pan'):
        audit('pan_lookup', 'employee', actor_id=user.id, after={'matches': len(out)})
    return out


def _public(id_: str, company_id: str | None = None) -> dict | None:
    qs = Employee.objects.filter(pk=id_) if company_id is None else Employee.objects.for_company(company_id).filter(pk=id_)
    return _row(public_rows(qs).first())


def get_employee(user: AuthUser, id_: str) -> dict:
    id_ = parse_uuid(str(id_))
    if not has_role(user, *READ_ALL):
        assert_manages(user, id_)
    emp = _public(id_, user.company_id)
    if not emp:
        raise NotFoundError('employee not found')
    return emp


def _require_hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


def sensitive(user: AuthUser, id_: str) -> dict:
    """Sensitive fields (PAN, full bank account) decrypted on demand, HR only, audited."""
    _require_hr(user)
    id_ = parse_uuid(str(id_))
    row = Employee.objects.for_company(user.company_id).filter(pk=id_).values('pan_enc', 'bank_account_enc').first()
    if not row:
        raise NotFoundError('employee not found')
    audit('read_sensitive', 'employee', actor_id=user.id, row_id=id_)
    c = field_cipher()
    return {'pan': c.decrypt(bytes(row['pan_enc']), f'employee:{id_}:pan') if row['pan_enc'] else None,
            'bankAccount': c.decrypt(bytes(row['bank_account_enc']), f'employee:{id_}:bank') if row['bank_account_enc'] else None}


def _masked(b: dict) -> dict:
    """The body for the audit log, with PAN and bank account replaced by *** (as the TS route)."""
    out = dict(b)
    for k in ('pan', 'bankAccount'):
        if out.get(k):
            out[k] = '***'
        else:
            out.pop(k, None)
    return out


def _secrets(id_: str, b: dict) -> dict:
    """Encrypted PAN (with its blind index) and bank account for an employee row; the AAD names the row."""
    c = field_cipher()
    out = {}
    if b.get('pan'):
        out.update(pan_enc=c.encrypt(b['pan'], f'employee:{id_}:pan'), pan_bidx=c.blind_index(b['pan']))
    if b.get('bankAccount'):
        out.update(bank_account_enc=c.encrypt(b['bankAccount'], f'employee:{id_}:bank'), bank_account_last4=b['bankAccount'][-4:])
    return out


def create_employee(user: AuthUser, body: dict) -> str:
    from hajiri.lifecycle.service import auto_start_onboarding
    from hajiri.people.career import record_career_event
    _require_hr(user)
    company_id = company_id_of(user)
    b = plain(validate(EmployeeIn, body))
    validate_refs(company_id, b)
    if b.get('pan'):
        assert_pan_free(company_id, b['pan'])
    with transaction.atomic():
        emp = Employee.objects.create(
            company_id=company_id, emp_code=b['empCode'], name_en=b['nameEn'], name_ne=b.get('nameNe'), gender=b.get('gender'),
            dob=b.get('dob'), marital_status=b.get('maritalStatus'), join_date=b.get('joinDate'), designation=b.get('designation'),
            grade=b.get('grade'), phone=b.get('phone'), email=b.get('email'), ot_eligible=b.get('otEligible', True),
            weekly_off_days=b.get('weeklyOffDays', [6]), regime=b.get('regime', Employee.Regime.SSF), ssf_id=b.get('ssfId'),
            pf_id=b.get('pfId'), cit_id=b.get('citId'), bank_code=b.get('bankCode'), bank_branch=b.get('bankBranch'),
            is_resident=b.get('isResident', True), remote_area_class=b.get('remoteAreaClass'), disability=b.get('disability', False),
            festival_month=b.get('festivalMonth'), probation_end=b.get('probationEnd'), work_from_home=b.get('workFromHome', False))
        id_ = str(emp.pk)
        secrets = _secrets(id_, b)
        if secrets:
            Employee.objects.filter(pk=id_).update(**secrets)
        frm = _d(b.get('joinDate')) or npt_today()
        EmployeeAssignment.objects.create(employee_id=id_, valid=DateRange(frm, None), branch_id=b.get('branchId'),
                                          org_unit_id=b.get('orgUnitId'), manager_id=b.get('managerId'),
                                          employment_type=b.get('employmentType', EmployeeAssignment.EmploymentType.REGULAR))
        if b.get('shiftId'):
            ShiftAssignment.objects.create(employee_id=id_, shift_id=b['shiftId'], valid=DateRange(frm, None))
        if b.get('designation'):
            link_designation(id_)
        details = {**({'designation': b['designation']} if b.get('designation') else {}), **({'grade': b['grade']} if b.get('grade') else {})}
        record_career_event(company_id=company_id, employee_id=id_, kind='hire', effective_date=frm, details=details, actor_id=user.id)
        audit('create', 'employee', actor_id=user.id, row_id=id_, after=_masked(b))
        auto_start_onboarding(company_id, id_, b.get('joinDate') or npt_today().isoformat(), user.id)
    return id_


_PATCH_MAP = [
    ('empCode', 'emp_code'), ('nameEn', 'name_en'), ('nameNe', 'name_ne'), ('gender', 'gender'), ('dob', 'dob'), ('maritalStatus', 'marital_status'),
    ('joinDate', 'join_date'), ('exitDate', 'exit_date'), ('designation', 'designation'), ('grade', 'grade'), ('phone', 'phone'), ('email', 'email'),
    ('otEligible', 'ot_eligible'), ('workFromHome', 'work_from_home'), ('weeklyOffDays', 'weekly_off_days'), ('regime', 'regime'),
    ('ssfId', 'ssf_id'), ('pfId', 'pf_id'), ('citId', 'cit_id'), ('bankCode', 'bank_code'), ('bankBranch', 'bank_branch'),
    ('isResident', 'is_resident'), ('remoteAreaClass', 'remote_area_class'), ('disability', 'disability'), ('festivalMonth', 'festival_month'),
    ('probationEnd', 'probation_end'),
]


def _move_assignment(id_: str, b: dict, today: date) -> None:
    """Effective-dated: close the current assignment today and open a new one (plan §9.1 decision 2)."""
    periods = EmployeeAssignment.objects.filter(employee_id=id_)
    cur = periods.filter(valid__contains=today).values('valid', 'branch_id', 'org_unit_id', 'manager_id', 'employment_type').first()
    nxt = {
        'branch_id': b.get('branchId') or (cur and cur['branch_id']) or None,
        'org_unit_id': b['orgUnitId'] if 'orgUnitId' in b else (cur and cur['org_unit_id']) or None,
        'manager_id': b['managerId'] if 'managerId' in b else (cur and cur['manager_id']) or None,
        'employment_type': b.get('employmentType') or (cur and cur['employment_type']) or EmployeeAssignment.EmploymentType.REGULAR,
    }
    if cur:
        if cur['valid'].lower == today:
            periods.filter(valid__contains=today).delete()
        else:
            periods.filter(valid__contains=today).update(valid=DateRange(cur['valid'].lower, today))
    EmployeeAssignment.objects.create(employee_id=id_, valid=DateRange(today, None), **nxt)


def _move_shift(id_: str, shift_id: str, today: date) -> None:
    periods = ShiftAssignment.objects.filter(employee_id=id_)
    cur = periods.filter(valid__contains=today, valid__startswith__lt=today).values_list('valid', flat=True).first()
    if cur:
        periods.filter(valid__contains=today, valid__startswith__lt=today).update(valid=DateRange(cur.lower, today))
    periods.filter(valid__startswith=today).delete()
    ShiftAssignment.objects.create(employee_id=id_, shift_id=shift_id, valid=DateRange(today, None))


def update_employee(user: AuthUser, id_: str, body: dict) -> dict:
    from hajiri.people.history import diff_employee_rows, record_field_changes
    from hajiri.people.service import people_policy
    _require_hr(user)
    id_ = parse_uuid(str(id_))
    company_id = company_id_of(user)
    b = plain(validate(EmployeeIn, body, partial=True))
    validate_refs(company_id, b, id_)
    before = _public(id_, company_id)
    if not before:
        raise NotFoundError('employee not found')
    if (b.get('pan') or b.get('bankAccount')) and people_policy(company_id)['makerChecker']:
        raise ConflictError("PAN and bank account changes need a second HR approval: edit them under the employee's profile (ID documents, Bank account)")
    stored_join = before['join_date']
    if b.get('exitDate') and stored_join and not b.get('joinDate') and _d(b['exitDate']) < stored_join:
        raise BadRequestError('exit date cannot be before join date')
    with transaction.atomic():
        sets = {col: b[k] for k, col in _PATCH_MAP if k in b}
        if b.get('exitDate'):
            sets['status'] = Status.EXITED
        elif 'exitDate' in b and b['exitDate'] is None:
            sets['status'] = Status.ACTIVE
        if b.get('pan'):
            assert_pan_free(company_id, b['pan'], id_)
        sets.update(_secrets(id_, b))
        if sets:
            Employee.objects.filter(pk=id_).update(**sets, updated_at=Now())
        today = npt_today()
        if any(k in b for k in ('branchId', 'orgUnitId', 'managerId', 'employmentType')):
            _move_assignment(id_, b, today)
        if b.get('shiftId'):
            _move_shift(id_, b['shiftId'], today)
        if 'designation' in b:
            link_designation(id_)
        after_row = _public(id_)
        changes = diff_employee_rows(before, after_row)
        if b.get('pan'):
            changes.append({'field': 'pan', 'label': 'PAN', 'from': 'Recorded' if before['has_pan'] else None, 'to': f"•••• {b['pan'][-4:]}"})
            changes = [x for x in changes if x['field'] != 'has_pan']
        record_field_changes(company_id=company_id, employee_id=id_, changes=changes, source='hr_edit', actor_id=user.id)
        audit('update', 'employee', actor_id=user.id, row_id=id_, before=before, after=_masked(b))
    return _public(id_)


def parse_csv(text: str) -> list[list[str]]:
    """RFC 4180 CSV (quotes, escaped quotes, CRLF) with the csv module; rows with nothing but blanks are dropped."""
    return [r for r in csv.reader(io.StringIO(text, newline='')) if any(f.strip() != '' for f in r)]


def _bool_text(v: str, *truthy: str) -> bool:
    return v.lower() in truthy


def import_csv(user: AuthUser, text: str | None, *, dry_run: bool = False) -> tuple[int, dict]:
    """Excel/CSV import with row-level validation (plan §13.8). Returns (http status, body) as the TS route."""
    from hajiri.people.career import record_career_event
    _require_hr(user)
    if not text:
        raise BadRequestError('send CSV as text/csv body or {"csv": "..."}')
    data = parse_csv(text.lstrip('﻿') if text.startswith('﻿') else text)
    if len(data) < 2:
        raise BadRequestError('CSV needs a header row and at least one data row')
    if len(data) > 5001:
        raise BadRequestError('CSV has more than 5,000 data rows; split the file')
    header = [h.strip() for h in data[0]]
    for h in ('emp_code', 'name_en'):
        if h not in header:
            raise BadRequestError(f'missing column {h}')
    errors: list[dict] = []
    valid: list[dict] = []
    columns = {'nameNe': 'name_ne', 'gender': 'gender', 'dob': 'dob', 'maritalStatus': 'marital_status', 'joinDate': 'join_date',
               'designation': 'designation', 'grade': 'grade', 'phone': 'phone', 'email': 'email', 'regime': 'regime',
               'ssfId': 'ssf_id', 'pan': 'pan', 'bankCode': 'bank_code', 'bankBranch': 'bank_branch', 'bankAccount': 'bank_account'}
    for i in range(1, len(data)):
        rec = {h: (data[i][j].strip() if j < len(data[i]) else '') for j, h in enumerate(header)}
        cand = {'empCode': rec.get('emp_code', ''), 'nameEn': rec.get('name_en', ''),
                **{k: rec[col] for k, col in columns.items() if rec.get(col)}}
        if rec.get('ot_eligible'):
            cand['otEligible'] = rec['ot_eligible'].lower() == 'true' or rec['ot_eligible'] == '1'
        if rec.get('work_from_home'):
            cand['workFromHome'] = _bool_text(rec['work_from_home'], 'true', '1', 'yes')
        if 'emp_code' not in rec:
            del cand['empCode']
        try:
            valid.append(plain(validate(EmployeeIn, cand)))
        except ValidationError as res:
            for iss in res.issues:
                errors.append({'row': i + 1, 'message': f"{iss['path']}: {iss['message']}"})
    codes = [v['empCode'] for v in valid]
    for d in dict.fromkeys(cd for j, cd in enumerate(codes) if codes.index(cd) != j):
        errors.append({'row': 0, 'message': f'duplicate emp_code in file: {d}'})
    pans = [v['pan'] for v in valid if v.get('pan')]
    for d in dict.fromkeys(p for j, p in enumerate(pans) if pans.index(p) != j):
        errors.append({'row': 0, 'message': f'duplicate PAN in file: *****{d[-4:]}'})
    company_id = company_id_of(user)
    for v in valid:
        if not v.get('pan'):
            continue
        taken = pan_owner(company_id, v['pan'])
        if taken:
            errors.append({'row': 0, 'message': f"PAN of {v['empCode']} is already recorded for {taken}"})
    if errors or dry_run:
        return (400 if errors else 200), {'imported': 0, 'valid': len(valid), 'errors': errors}
    imported = 0
    with transaction.atomic():
        for b in valid:
            try:
                with transaction.atomic():           # a savepoint: a duplicate code skips this row only
                    emp = Employee.objects.create(
                        company_id=company_id, emp_code=b['empCode'], name_en=b['nameEn'], name_ne=b.get('nameNe'), gender=b.get('gender'),
                        dob=b.get('dob'), marital_status=b.get('maritalStatus'), join_date=b.get('joinDate'),
                        designation=b.get('designation'), grade=b.get('grade'), phone=b.get('phone'), email=b.get('email'),
                        ot_eligible=b.get('otEligible', True), regime=b.get('regime', Employee.Regime.SSF), ssf_id=b.get('ssfId'),
                        bank_code=b.get('bankCode'), bank_branch=b.get('bankBranch'), work_from_home=b.get('workFromHome', False))
            except IntegrityError as e:
                if getattr(getattr(e, '__cause__', None), 'sqlstate', None) != '23505':
                    raise
                errors.append({'row': 0, 'message': f"emp_code {b['empCode']} already exists; skipped"})
                continue
            id_ = str(emp.pk)
            secrets = _secrets(id_, b)
            if secrets:
                Employee.objects.filter(pk=id_).update(**secrets)
            frm = _d(b.get('joinDate')) or npt_today()
            EmployeeAssignment.objects.create(employee_id=id_, valid=DateRange(frm, None),
                                              employment_type=EmployeeAssignment.EmploymentType.REGULAR)
            if b.get('designation'):
                link_designation(id_)
            details = {**({'designation': b['designation']} if b.get('designation') else {}), **({'grade': b['grade']} if b.get('grade') else {})}
            record_career_event(company_id=company_id, employee_id=id_, kind='hire', effective_date=frm, details=details, actor_id=user.id)
            imported += 1
        audit('import', 'employee', actor_id=user.id, after={'imported': imported, 'skipped': len(errors)})
    return 201, {'imported': imported, 'errors': errors}


def _range_text(r) -> str:
    """A daterange as PostgreSQL prints it: [2025-01-01,) or [2025-01-01,2026-01-01)."""
    lo = r.lower.isoformat() if r.lower else ''
    hi = r.upper.isoformat() if r.upper else ''
    return f"{'[' if r.lower_inc else '('}{lo},{hi}{']' if r.upper_inc else ')'}"


def assignments(user: AuthUser, id_: str) -> list[dict]:
    """Effective-dated assignment history."""
    id_ = parse_uuid(str(id_))
    assert_manages(user, id_)
    qs = (EmployeeAssignment.objects.for_company(company_id_of(user)).filter(employee_id=id_)
          .annotate(branch_name=F('branch__name_en'), manager_name=F('manager__name_en')).order_by('valid__startswith')
          .values('employee_id', 'valid', 'branch_id', 'org_unit_id', 'manager_id', 'employment_type', 'probation_end',
                  'branch_name', 'manager_name'))
    out = []
    for r in qs:
        v = r.pop('valid')
        out.append({**r, 'valid': _range_text(v), 'valid_from': v.lower, 'valid_to': v.upper, 'employee_id': str(r['employee_id']),
                    'branch_id': r['branch_id'] and str(r['branch_id']), 'org_unit_id': r['org_unit_id'] and str(r['org_unit_id']),
                    'manager_id': r['manager_id'] and str(r['manager_id'])})
    return out


def audit_events(user: AuthUser, id_: str) -> list[dict]:
    """Employee-visible audit of their own record (plan §5.11)."""
    id_ = parse_uuid(str(id_))
    assert_manages(user, id_)
    actor = AppUser.objects.filter(pk=OuterRef('actor_id')).values('name')[:1]
    return list(AuditLog.objects.filter(row_id=id_).annotate(actor_name=Subquery(actor)).order_by('-id')
                .values('id', 'occurred_at', 'actor_id', 'action', 'table_name', 'row_id', 'actor_name')[:200])
