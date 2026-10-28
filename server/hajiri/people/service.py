"""
People and core HR (port of apps/api/src/modules/people/routes.ts): org structure, directory and org chart for
everyone, the full employee profile (self and HR), career events, profile change requests and field-level history.
Every function takes the signed-in AuthUser and a body in the TS camelCase shape, and raises hajiri.core.errors.
Everything is scoped to the ACTIVE company (company_id_of(user)); ids of another company are not found.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import (BooleanField, Case, Count, ExpressionWrapper, F, FilteredRelation, Func, JSONField, OuterRef, Q,
                              Subquery, TextField, Value, When)
from django.db.models.functions import Coalesce, JSONObject, Now

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role, is_manager_of
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import npt_today
from hajiri.core.collation import collate_key
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.scoping import as_uuid, is_uuid
from hajiri.core.settings_store import get_setting, put_setting
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (Branch, CareerEvent, Designation, Employee, EmployeeAssignment, EmployeeFieldHistory,
                              EmployeeTempAssignment, OrgUnit, ProfileChangeRequest, WorkflowInstance)

from . import career as career_mod
from .change_requests import PROFILE_CHANGE, REQ_COLS, create_change_request, decrypt_payload, req_row, req_values
from .filters import DirectoryFilter
from .profile import SECRET_FIELDS, SECTION_LABEL, SENSITIVE_SECTIONS, apply_section, diff_section, parse_section, read_profile
from .serializers import (REQUEST_STATUSES, AvatarIn, BranchIn, ChangeIn, DesignationIn, DirectoryQuery, PolicyIn, UnitIn,
                          one_of)

PEOPLE_POLICY = 'people.policy'
ACTIVE = Employee.Status.ACTIVE
RequestStatus = ProfileChangeRequest.Status


def people_policy(company_id: str) -> dict:
    return validate(PolicyIn, get_setting(company_id, PEOPLE_POLICY) or {})


def _sid(r: dict | None, *keys: str) -> dict | None:
    if r is None:
        return None
    for k in keys:
        if r.get(k) is not None:
            r[k] = str(r[k])
    return r


def employee_in(company_id: str, id_: str) -> dict:
    """Same company, else 404 (never reveal other companies' ids)."""
    e = (Employee.objects.for_company(company_id).filter(pk=id_).values('id', 'name_en', 'status', 'avatar_file_id').first()
         if is_uuid(id_) else None)
    if not e:
        raise NotFoundError('employee not found')
    return _sid(e, 'id', 'avatar_file_id')


def self_or_hr(user: AuthUser, employee_id: str) -> None:
    """The employee themself or HR."""
    if has_role(user, 'hr') or user.employee_id == str(employee_id):
        return
    raise ForbiddenError('only the employee and HR can see this')


def _hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


def _flag(q: Q) -> ExpressionWrapper:
    return ExpressionWrapper(q, output_field=BooleanField())


def with_current_assignment(qs, on=None):
    """Employees with their assignment on a Nepal date (default today) joined as `a` (a LEFT JOIN on the period that
    covers the date): F('a__branch_id'), F('a__manager__name_en')..."""
    on = on or npt_today()
    return qs.annotate(a=FilteredRelation('employee_assignments', condition=Q(employee_assignments__valid__contains=on)))


# ---------------------------------------------------------------- directory, summary, org chart (everyone)

_DIR_IDS = ('id', 'branch_id', 'org_unit_id', 'manager_id')


def _temp_assignment(today):
    """The acting or deputation assignment running today, as {kind, label, until}."""
    t = (EmployeeTempAssignment.objects.filter(employee=OuterRef('pk'), status=EmployeeTempAssignment.Status.ACTIVE,
                                               start_date__lte=today, end_date__gte=today)
         .order_by('-start_date')
         .annotate(j=JSONObject(kind='kind', label=Coalesce('designation', 'branch__name_en', 'org_unit__name_en'),
                                until='end_date'))
         .values('j')[:1])
    return Subquery(t, output_field=JSONField())


def directory_rows(company_id: str, *extra: str):
    """The directory card columns (DIRECTORY_COLS of the TS route) for a company's employees."""
    today = npt_today()
    return (with_current_assignment(Employee.objects.for_company(company_id), today)
            .annotate(work_email=F('email'), has_avatar=_flag(Q(avatar_file__isnull=False)), branch_id=F('a__branch_id'),
                      branch_name=F('a__branch__name_en'), org_unit_id=F('a__org_unit_id'), unit_name=F('a__org_unit__name_en'),
                      manager_id=F('a__manager_id'), manager_name=F('a__manager__name_en'), temp_assignment=_temp_assignment(today),
                      skills_text=Func(F('skills'), Value(' '), function='array_to_string', output_field=TextField()))
            .values('id', 'emp_code', 'name_en', 'designation', 'work_email', 'work_phone', 'join_date', 'has_avatar', 'branch_id',
                    'branch_name', 'org_unit_id', 'unit_name', 'manager_id', 'manager_name', 'temp_assignment', *extra))


def directory(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = plain(validate(DirectoryQuery, query or {}))
    qs = directory_rows(company_id_of(user)).filter(status=ACTIVE)
    qs = DirectoryFilter(q, queryset=qs).qs.order_by('name_en')
    return [_sid(r, *_DIR_IDS) for r in qs[:1000]]


def summary(user: AuthUser) -> dict:
    company_id = company_id_of(user)
    today = npt_today()
    active = Q(status=ACTIVE)
    t = (with_current_assignment(Employee.objects.for_company(company_id), today)
         .annotate(probation=Coalesce(F('a__probation_end'), F('probation_end')))
         .aggregate(active=Count('id', filter=active),
                    joined_30d=Count('id', filter=active & Q(join_date__gt=today - timedelta(days=30))),
                    on_probation=Count('id', filter=active & Q(confirmed_on__isnull=True, probation__gte=today)),
                    leaving=Count('id', filter=active & Q(exit_date__gte=today))))
    current = Q(employee_assignments__valid__contains=today, employee_assignments__employee__status=ACTIVE)
    by_unit = [_sid(r, 'id') for r in OrgUnit.objects.for_company(company_id)
               .annotate(name=F('name_en'), count=Count('employee_assignments', filter=current))
               .order_by('-count', 'name_en').values('id', 'name', 'count')]
    by_branch = [_sid(r, 'id') for r in Branch.objects.for_company(company_id)
                 .annotate(name=F('name_en'), count=Count('employee_assignments', filter=current))
                 .order_by('-count', 'name_en').values('id', 'name', 'count')]
    return {'active': t['active'], 'joinedLast30Days': t['joined_30d'], 'onProbation': t['on_probation'], 'leaving': t['leaving'],
            'byUnit': by_unit, 'byBranch': by_branch}


def org_chart(user: AuthUser) -> list[dict]:
    qs = (with_current_assignment(Employee.objects.for_company(company_id_of(user)).filter(status=ACTIVE))
          .annotate(has_avatar=_flag(Q(avatar_file__isnull=False)), unit_name=F('a__org_unit__name_en'),
                    # A manager who has left is no manager: the person becomes a root of the chart.
                    manager_id=Case(When(a__manager__status=ACTIVE, then=F('a__manager_id')), default=None))
          .order_by('name_en').values('id', 'name_en', 'designation', 'has_avatar', 'unit_name', 'manager_id'))
    return [_sid(r, 'id', 'manager_id') for r in qs]


def build_tree(nodes: list[dict]) -> dict:
    """The reporting tree of OrgChartPage.tsx: anyone whose manager is missing (or would form a loop) becomes a root."""
    by_id = {n['id']: n for n in nodes}
    parent = {n['id']: n['manager_id'] for n in nodes
              if n.get('manager_id') and n['manager_id'] in by_id and n['manager_id'] != n['id']}
    for n in nodes:
        seen = {n['id']}
        cur = parent.get(n['id'])
        while cur:
            if cur in seen:
                parent.pop(n['id'], None)
                break
            seen.add(cur)
            cur = parent.get(cur)
    children: dict[str, list[dict]] = {}
    roots = []
    for n in nodes:
        p = parent.get(n['id'])
        if p:
            children.setdefault(p, []).append(n)
        else:
            roots.append(n)
    size: dict[str, int] = {}

    def count(i: str) -> int:
        if i in size:
            return size[i]
        size[i] = 0
        s = sum(1 + count(c['id']) for c in children.get(i, []))
        size[i] = s
        return s
    for n in nodes:
        count(n['id'])
    roots.sort(key=lambda r: (-size.get(r['id'], 0), collate_key(r['name_en'])))
    return {'roots': roots, 'children': children, 'parent': parent, 'size': size}


# ---------------------------------------------------------------- org structure

def _headcount(path: str, today):
    """Active people whose current assignment points here (`path` is the reverse relation to employee_assignment)."""
    return Count(path, filter=Q(**{f'{path}__valid__contains': today, f'{path}__employee__status': ACTIVE}))


def structure(user: AuthUser) -> dict:
    company_id = company_id_of(user)
    today = npt_today()
    branches = [_sid(r, 'id', 'head_employee_id') for r in Branch.objects.for_company(company_id)
                .annotate(head_name=F('head_employee__name_en'), headcount=_headcount('employee_assignments', today))
                .order_by('code').values('id', 'code', 'name_en', 'address', 'phone', 'head_employee_id', 'head_name', 'headcount')]
    units = [_sid(r, 'id', 'parent_id', 'branch_id', 'head_employee_id') for r in OrgUnit.objects.for_company(company_id)
             .annotate(head_name=F('head_employee__name_en'), headcount=_headcount('employee_assignments', today))
             .order_by('name_en').values('id', 'code', 'name_en', 'kind', 'parent_id', 'branch_id', 'description', 'head_employee_id',
                                         'head_name', 'headcount')]
    designations = [_sid(r, 'id', 'org_unit_id') for r in Designation.objects.for_company(company_id)
                    .annotate(headcount=Count('employees', filter=Q(employees__status=ACTIVE)))
                    .order_by(F('job_level').asc(nulls_last=True), 'name_en')
                    .values('id', 'code', 'name_en', 'grade', 'job_level', 'org_unit_id', 'description', 'active', 'headcount')]
    return {'branches': branches, 'units': units, 'designations': designations}


def _owned_or_null(company_id: str, model, id_, label: str) -> None:
    if id_ and not model.objects.for_company(company_id).filter(pk=id_).exists():
        raise BadRequestError(f'{label} does not exist in your company')


def _unique_violation(e: Exception) -> bool:
    return getattr(getattr(e, '__cause__', None), 'sqlstate', None) == '23505'


def _fk_violation(e: Exception) -> bool:
    return getattr(getattr(e, '__cause__', None), 'sqlstate', None) == '23503'


def _dup(what: str):
    return ConflictError(f'another {what} already uses this code')


def _fields(b: dict, mapping: dict[str, str]) -> dict:
    """The model fields for the body keys that were sent (a partial update changes only those)."""
    return {col: b[k] for k, col in mapping.items() if k in b}


BRANCH_FIELDS = {'code': 'code', 'nameEn': 'name_en', 'address': 'address', 'phone': 'phone', 'headEmployeeId': 'head_employee_id'}
UNIT_FIELDS = {'kind': 'kind', 'code': 'code', 'nameEn': 'name_en', 'parentId': 'parent_id', 'branchId': 'branch_id',
               'headEmployeeId': 'head_employee_id', 'description': 'description'}
DESIGNATION_FIELDS = {'code': 'code', 'nameEn': 'name_en', 'grade': 'grade', 'jobLevel': 'job_level', 'orgUnitId': 'org_unit_id',
                      'description': 'description', 'active': 'active'}


def _saved(model, pk, *cols: str) -> dict:
    return _sid(model.objects.filter(pk=pk).values('id', *cols).get(), 'id')


def create_branch(user: AuthUser, body: dict) -> dict:
    _hr(user)
    company_id = company_id_of(user)
    b = plain(validate(BranchIn, body))
    _owned_or_null(company_id, Employee, b.get('headEmployeeId'), 'branch head')
    try:
        with transaction.atomic():
            row = Branch.objects.create(company_id=company_id, **_fields(b, BRANCH_FIELDS))
            audit('create', 'branch', actor_id=user.id, row_id=str(row.pk), after=b)
    except IntegrityError as e:
        if _unique_violation(e):
            raise _dup('branch') from e
        raise
    return _saved(Branch, row.pk, 'code', 'name_en')


def update_branch(user: AuthUser, id_: str, body: dict) -> dict:
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    b = plain(validate(BranchIn, body, partial=True))
    _owned_or_null(company_id, Employee, b.get('headEmployeeId'), 'branch head')
    try:
        with transaction.atomic():
            qs = Branch.objects.for_company(company_id).filter(pk=id_)
            if not (qs.update(**_fields(b, BRANCH_FIELDS)) if _fields(b, BRANCH_FIELDS) else qs.exists()):
                raise NotFoundError('branch not found')
            audit('update', 'branch', actor_id=user.id, row_id=id_, after=b)
    except IntegrityError as e:
        if _unique_violation(e):
            raise _dup('branch') from e
        raise
    return _saved(Branch, id_, 'code', 'name_en')


def delete_branch(user: AuthUser, id_: str) -> None:
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    branch = Branch.objects.for_company(company_id).filter(pk=id_)
    if not branch.exists():
        raise NotFoundError('branch not found')
    if EmployeeAssignment.objects.filter(branch_id=id_).exists():
        raise ConflictError('people are (or were) assigned to this branch; move them before deleting it')
    try:
        with transaction.atomic():
            n, _ = branch.delete()
    except IntegrityError as e:
        if _fk_violation(e):
            raise ConflictError('this branch is still used by other records and cannot be deleted') from e
        raise
    if not n:
        raise NotFoundError('branch not found')
    audit('delete', 'branch', actor_id=user.id, row_id=id_)


def _assert_no_cycle(company_id: str, self_id: str, parent_id) -> None:
    """A unit may not sit under itself or under one of its own sub-units."""
    parents = dict(OrgUnit.objects.for_company(company_id).values_list('id', 'parent_id'))
    cur = parent_id
    guard = 0
    while cur and guard < 100:
        guard += 1
        if str(cur) == str(self_id):
            raise BadRequestError('a unit cannot sit under itself or one of its own sub-units')
        cur = parents.get(as_uuid(cur))


def create_unit(user: AuthUser, body: dict) -> dict:
    _hr(user)
    company_id = company_id_of(user)
    b = plain(validate(UnitIn, body))
    _owned_or_null(company_id, OrgUnit, b.get('parentId'), 'parent unit')
    _owned_or_null(company_id, Branch, b.get('branchId'), 'branch')
    _owned_or_null(company_id, Employee, b.get('headEmployeeId'), 'unit head')
    try:
        with transaction.atomic():
            row = OrgUnit.objects.create(company_id=company_id, **_fields(b, UNIT_FIELDS))
            audit('create', 'org_unit', actor_id=user.id, row_id=str(row.pk), after=b)
    except IntegrityError as e:
        if _unique_violation(e):
            raise _dup('department or team') from e
        raise
    return _saved(OrgUnit, row.pk, 'code', 'name_en', 'kind')


def update_unit(user: AuthUser, id_: str, body: dict) -> dict:
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    b = plain(validate(UnitIn, body, partial=True))
    _owned_or_null(company_id, OrgUnit, b.get('parentId'), 'parent unit')
    _owned_or_null(company_id, Branch, b.get('branchId'), 'branch')
    _owned_or_null(company_id, Employee, b.get('headEmployeeId'), 'unit head')
    if 'parentId' in b:
        _assert_no_cycle(company_id, id_, b['parentId'])
    try:
        with transaction.atomic():
            qs = OrgUnit.objects.for_company(company_id).filter(pk=id_)
            sets = _fields(b, UNIT_FIELDS)
            if not (qs.update(**sets) if sets else qs.exists()):
                raise NotFoundError('unit not found')
            audit('update', 'org_unit', actor_id=user.id, row_id=id_, after=b)
    except IntegrityError as e:
        if _unique_violation(e):
            raise _dup('department or team') from e
        raise
    return _saved(OrgUnit, id_, 'code', 'name_en', 'kind')


def delete_unit(user: AuthUser, id_: str) -> None:
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    unit = OrgUnit.objects.for_company(company_id).filter(pk=id_)
    if not unit.exists():
        raise NotFoundError('unit not found')
    if OrgUnit.objects.filter(parent_id=id_).exists():
        raise ConflictError('this unit has sub-units; move or delete them first')
    if EmployeeAssignment.objects.filter(org_unit_id=id_).exists():
        raise ConflictError('people are (or were) assigned to this unit; move them before deleting it')
    try:
        with transaction.atomic():
            Designation.objects.for_company(company_id).filter(org_unit_id=id_).update(org_unit=None)
            n, _ = unit.delete()
            if n:
                audit('delete', 'org_unit', actor_id=user.id, row_id=id_)
    except IntegrityError as e:
        if _fk_violation(e):
            raise ConflictError('this unit is still used by other records and cannot be deleted') from e
        raise
    if not n:
        raise NotFoundError('unit not found')


def create_designation(user: AuthUser, body: dict) -> dict:
    _hr(user)
    company_id = company_id_of(user)
    b = plain(validate(DesignationIn, body))
    _owned_or_null(company_id, OrgUnit, b.get('orgUnitId'), 'department')
    try:
        with transaction.atomic():
            row = Designation.objects.create(company_id=company_id, **{'active': True, **_fields(b, DESIGNATION_FIELDS)})
            audit('create', 'designation', actor_id=user.id, row_id=str(row.pk), after=b)
    except IntegrityError as e:
        if _unique_violation(e):
            raise _dup('designation') from e
        raise
    return _saved(Designation, row.pk, 'code', 'name_en', 'grade', 'job_level')


def update_designation(user: AuthUser, id_: str, body: dict) -> dict:
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    b = plain(validate(DesignationIn, body, partial=True))
    _owned_or_null(company_id, OrgUnit, b.get('orgUnitId'), 'department')
    with transaction.atomic():
        qs = Designation.objects.for_company(company_id).filter(pk=id_)
        sets = _fields(b, DESIGNATION_FIELDS)
        try:
            with transaction.atomic():
                found = qs.update(**sets) if sets else qs.exists()
        except IntegrityError as e:
            if _unique_violation(e):
                raise _dup('designation') from e
            raise
        if not found:
            raise NotFoundError('designation not found')
        # Renaming a designation renames it on everyone who holds it.
        if b.get('nameEn'):
            (Employee.objects.for_company(company_id).filter(designation_ref_id=id_).exclude(designation=b['nameEn'])
             .update(designation=b['nameEn'], updated_at=Now()))
        audit('update', 'designation', actor_id=user.id, row_id=id_, after=b)
    return _saved(Designation, id_, 'code', 'name_en', 'grade', 'job_level')


def delete_designation(user: AuthUser, id_: str) -> None:
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    designation = Designation.objects.for_company(company_id).filter(pk=id_)
    if not designation.exists():
        raise NotFoundError('designation not found')
    if Employee.objects.filter(designation_ref_id=id_).exists():
        raise ConflictError('people hold this designation; mark it inactive instead')
    try:
        with transaction.atomic():
            n, _ = designation.delete()
    except IntegrityError as e:
        if _fk_violation(e):
            raise ConflictError('this designation is still used by other records and cannot be deleted') from e
        raise
    if not n:
        raise NotFoundError('designation not found')
    audit('delete', 'designation', actor_id=user.id, row_id=id_)


# ---------------------------------------------------------------- policy

def get_policy(user: AuthUser) -> dict:
    _hr(user)
    return people_policy(company_id_of(user))


def set_policy(user: AuthUser, body: dict) -> dict:
    _hr(user)
    company_id = company_id_of(user)
    p = validate(PolicyIn, body)
    before = people_policy(company_id)
    with transaction.atomic():
        put_setting(company_id, PEOPLE_POLICY, p, updated_by=user.id)
        audit('update', 'company_setting', actor_id=user.id, row_id=PEOPLE_POLICY, before=before, after=p)
    return p


# ---------------------------------------------------------------- my change requests and photo

def _with_label(r: dict) -> dict:
    return {**r, 'sectionLabel': SECTION_LABEL.get(r['section'], r['section'])}


def request_change(user: AuthUser, body: dict) -> dict:
    """POST /people/me/change-requests."""
    from hajiri.files.store import attach_file
    company_id = company_id_of(user)
    employee_id = user.employee_id
    if not employee_id:
        raise ForbiddenError('only employees have a profile to change')
    b = plain(validate(ChangeIn, body))
    section, value = parse_section(b['section'], b.get('value'))
    with transaction.atomic():
        if b.get('proofFileId'):
            attach_file(company_id=company_id, file_id=b['proofFileId'], user_id=user.id, purpose='profile_change',
                        attached_to=f'profile_change:{employee_id}')
        created = create_change_request(company_id=company_id, employee_id=employee_id, requested_by=user.id, section=section, value=value,
                                        reason=b.get('reason'), proof_file_id=b.get('proofFileId'), by_hr=False)
        audit('create', 'profile_change_request', actor_id=user.id, row_id=created['id'],
              after={'section': section, 'fields': [x['label'] for x in created['summary'].get('changes') or []]})
    return _with_label(created)


def my_requests(user: AuthUser) -> list[dict]:
    if not user.employee_id:
        return []
    qs = ProfileChangeRequest.objects.for_company(company_id_of(user)).filter(employee_id=user.employee_id).order_by('-created_at')
    return [_with_label(req_row(r)) for r in req_values(qs)[:100]]


def cancel_my_request(user: AuthUser, id_: str) -> None:
    from hajiri.workflow.services import cancel_workflow
    id_ = parse_uuid(str(id_))
    company_id = company_id_of(user)
    with transaction.atomic():
        x = ProfileChangeRequest.objects.for_company(company_id).select_for_update().filter(pk=id_).first()
        if not x or (str(x.employee_id) != user.employee_id and str(x.requested_by_id) != user.id):
            raise NotFoundError('change request not found')
        if x.status != RequestStatus.PENDING:
            raise ConflictError(f'this request is already {x.status}')
        ProfileChangeRequest.objects.filter(pk=id_).update(status=RequestStatus.CANCELLED, decided_at=Now(), decided_by_id=user.id)
        cancel_workflow(PROFILE_CHANGE, id_)


def set_avatar(user: AuthUser, employee_id: str, file_id: str | None) -> None:
    from hajiri.files.store import attach_file, delete_file
    company_id = company_id_of(user)
    with transaction.atomic():
        emp = Employee.objects.for_company(company_id).select_for_update().filter(pk=employee_id).values('avatar_file_id').first()
        if not emp:
            raise NotFoundError('employee not found')
        if file_id:
            attach_file(company_id=company_id, file_id=file_id, user_id=user.id, purpose='avatar', attached_to=f'employee_avatar:{employee_id}')
        old = emp['avatar_file_id'] and str(emp['avatar_file_id'])
        Employee.objects.filter(pk=employee_id).update(avatar_file_id=file_id, updated_at=Now())
        if old and old != file_id:
            delete_file(company_id, old)
        audit('update' if file_id else 'delete', 'employee_avatar', actor_id=user.id, row_id=employee_id)


def set_my_avatar(user: AuthUser, body: dict | None) -> None:
    if not user.employee_id:
        raise ForbiddenError('only employees have a profile photo')
    set_avatar(user, user.employee_id, None if body is None else plain(validate(AvatarIn, body))['fileId'])


def set_employee_avatar(user: AuthUser, employee_id: str, body: dict | None) -> None:
    _hr(user)
    employee_id = parse_uuid(str(employee_id))
    set_avatar(user, employee_id, None if body is None else plain(validate(AvatarIn, body))['fileId'])


# ---------------------------------------------------------------- change requests (HR)

def change_requests(user: AuthUser, status: str = 'pending') -> list[dict]:
    _hr(user)
    status = one_of(REQUEST_STATUSES, status or 'pending')
    qs = ProfileChangeRequest.objects.for_company(company_id_of(user))
    if status != 'all':
        qs = qs.filter(status=status)
    instance = WorkflowInstance.objects.filter(object_type=PROFILE_CHANGE, object_id=OuterRef('pk')).values('id')[:1]
    qs = (qs.annotate(employee_name=F('employee__name_en'), emp_code=F('employee__emp_code'),
                      requested_by_name=F('requested_by__name'), instance_id=Subquery(instance))
          .order_by('-created_at').values(*REQ_COLS, 'employee_name', 'emp_code', 'requested_by_name', 'instance_id'))
    return [_with_label(req_row(r)) for r in qs[:300]]


def load_request(user: AuthUser, id_: str) -> dict:
    id_ = parse_uuid(str(id_))
    row = ProfileChangeRequest.objects.for_company(company_id_of(user)).filter(pk=id_).values(*REQ_COLS, 'payload_enc').first()
    if not row:
        raise NotFoundError('change request not found')
    if not has_role(user, 'hr') and str(row['employee_id']) != user.employee_id:
        raise NotFoundError('change request not found')
    return row


def request_detail(user: AuthUser, id_: str, *, reveal: bool = False) -> dict:
    row = load_request(user, id_)
    reveal = reveal and has_role(user, 'hr')
    if row['status'] == RequestStatus.PENDING:
        changes = diff_section(str(row['employee_id']), row['section'], decrypt_payload(row), reveal=reveal)
    else:
        changes = (row['summary'] or {}).get('changes') or []
    if reveal:
        audit('read_sensitive', 'profile_change_request', actor_id=user.id, row_id=str(row['id']))
    rest = req_row({k: v for k, v in row.items() if k != 'payload_enc'})
    return {'request': _with_label(rest), 'changes': changes}


def request_proof(user: AuthUser, id_: str) -> dict:
    """The proof file of a change request (decrypted), for the employee and HR; HR's views are audited."""
    from hajiri.files.store import read_file
    row = load_request(user, id_)
    if not row['proof_file_id']:
        raise NotFoundError('no proof attached')
    if str(row['employee_id']) != user.employee_id:
        audit('view_file', 'profile_change_request', actor_id=user.id, row_id=str(row['id']))
    return read_file(str(row['company_id']), str(row['proof_file_id']))


# ---------------------------------------------------------------- one person

def card(user: AuthUser, id_: str) -> dict:
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    p = directory_rows(company_id, 'skills', 'languages', 'status').filter(pk=id_).first()
    if not p:
        raise NotFoundError('person not found')
    today = npt_today()
    reports = [_sid(r, 'id') for r in Employee.objects.for_company(company_id)
               .filter(status=ACTIVE, employee_assignments__manager_id=id_, employee_assignments__valid__contains=today)
               .annotate(has_avatar=_flag(Q(avatar_file__isnull=False)))
               .order_by('name_en').values('id', 'name_en', 'designation', 'has_avatar')]
    p = _sid(p, *_DIR_IDS)
    p.pop('skills_text', None)
    p['skills'] = list(p['skills'] or [])
    p['languages'] = list(p['languages'] or [])
    return {'person': p, 'reports': reports}


def avatar_file(user: AuthUser, id_: str) -> dict:
    from hajiri.files.store import read_file
    company_id = company_id_of(user)
    e = employee_in(company_id, parse_uuid(str(id_)))
    if not e['avatar_file_id']:
        raise NotFoundError('no profile photo')
    return read_file(company_id, e['avatar_file_id'])


def profile(user: AuthUser, id_: str, *, reveal: bool = False) -> dict:
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    employee_in(company_id, id_)
    self_or_hr(user, id_)
    if reveal:
        audit('read_sensitive', 'employee_profile', actor_id=user.id, row_id=id_)
    pending = [_sid(r, 'id') for r in ProfileChangeRequest.objects.for_company(company_id)
               .filter(employee_id=id_, status=RequestStatus.PENDING).values('id', 'section', 'created_at')]
    return {'profile': read_profile(id_, reveal=reveal), 'pending': pending, 'secretFields': list(SECRET_FIELDS)}


def save_section(user: AuthUser, id_: str, section: str, body) -> dict:
    """PUT /people/:id/profile/:section (HR). With maker-checker on, bank and ID edits become a request (pending)."""
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    employee_in(company_id, id_)
    section, value = parse_section(str(section), body)
    policy = people_policy(company_id)
    if policy['makerChecker'] and section in SENSITIVE_SECTIONS:
        with transaction.atomic():
            created = create_change_request(company_id=company_id, employee_id=id_, requested_by=user.id, section=section, value=value,
                                            reason='Edited by HR', by_hr=True)
            audit('create', 'profile_change_request', actor_id=user.id, row_id=created['id'], after={'section': section, 'byHr': True})
        return {'pending': True, 'request': created}
    with transaction.atomic():
        Employee.objects.select_for_update().filter(pk=id_).values_list('id', flat=True).first()   # one HR edit at a time
        ch = apply_section(company_id=company_id, employee_id=id_, section=section, value=value, actor_id=user.id, source='hr_edit')
        if ch:
            audit('update', 'employee_profile', actor_id=user.id, row_id=id_, after={'section': section, 'fields': [x['label'] for x in ch]})
    return {'pending': False, 'changes': ch}


def history(user: AuthUser, id_: str) -> list[dict]:
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    employee_in(company_id, id_)
    self_or_hr(user, id_)
    return list(EmployeeFieldHistory.objects.for_company(company_id).filter(employee_id=id_)
                .annotate(changed_by_name=F('changed_by__name')).order_by('-changed_at', '-id')
                .values('id', 'field', 'old_value', 'new_value', 'source', 'ref_id', 'changed_at', 'changed_by_name')[:500])


_DETAIL_NAMES = {'designationId': Designation, 'branchId': Branch, 'orgUnitId': OrgUnit, 'managerId': Employee}


def career(user: AuthUser, id_: str) -> list[dict]:
    """GET /people/:id/career: HR and the employee see everything; their manager sees the events without reasons,
    separation types, salary revisions or suspensions."""
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    employee_in(company_id, id_)
    full = has_role(user, 'hr') or user.employee_id == id_
    if not full and not (has_role(user, 'manager') and user.employee_id and is_manager_of(user.employee_id, id_)):
        raise ForbiddenError('not allowed to see this career history')
    evs = (CareerEvent.objects.for_company(company_id).filter(employee_id=id_).exclude(status=CareerEvent.Status.CANCELLED)
           .annotate(created_by_name=F('created_by__name')).order_by('-effective_date', '-created_at')
           .values('id', 'kind', 'effective_date', 'status', 'details', 'note', 'created_by_name', 'created_at'))
    names: dict[str, str] = {}

    def lookup(key: str, id_value) -> str | None:
        if not id_value:
            return None
        if id_value not in names:
            names[id_value] = (_DETAIL_NAMES[key].objects.filter(pk=id_value).values_list('name_en', flat=True).first()
                               if is_uuid(id_value) else None) or '—'
        return names[id_value]
    out = []
    for ev in evs:
        d = ev['details'] or {}
        parts: list[str] = []
        designation = d.get('designation') or lookup('designationId', d.get('designationId'))
        if designation:
            parts.append(designation)
        if d.get('grade'):
            parts.append(f"grade {d['grade']}")
        branch = lookup('branchId', d.get('branchId'))
        if branch:
            parts.append(f'{branch} branch')
        unit = lookup('orgUnitId', d.get('orgUnitId'))
        if unit:
            parts.append(unit)
        if 'managerId' in d:
            parts.append(f"reports to {lookup('managerId', d['managerId']) or 'nobody'}")
        if d.get('employmentType'):
            parts.append(d['employmentType'].replace('_', '-'))
        # Managers see the event but not separation types, salary revisions or reasons.
        if full and d.get('separationType'):
            parts.append(d['separationType'].replace('_', ' '))
        if full and 'revisionPercent' in d:
            rp = d['revisionPercent']
            rp_s = str(int(rp)) if float(rp).is_integer() else str(rp)
            parts.append(f"{'+' if rp > 0 else ''}{rp_s}%")
        if d.get('endDate'):
            parts.append(f"until {bs_label(d['endDate'])}")
        out.append({
            'id': str(ev['id']), 'kind': ev['kind'], 'label': career_mod.CAREER_LABEL[ev['kind']], 'effectiveDate': ev['effective_date'],
            'status': ev['status'], 'summary': ', '.join(parts), 'note': ev['note'] if full else None,
            'reason': d.get('reason') if full else None, 'createdBy': ev['created_by_name'], 'createdAt': ev['created_at'],
        })
    hidden = (CareerEvent.Kind.SALARY_REVISION, CareerEvent.Kind.SUSPENSION)
    return out if full else [e for e in out if e['kind'] not in hidden]


def record_career(user: AuthUser, id_: str, body: dict) -> dict:
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    b = career_mod.parse_career(body)
    with transaction.atomic():
        ev = career_mod.record_career_event(company_id=company_id, employee_id=id_, kind=b['kind'], effective_date=b['effectiveDate'],
                                            details=b['details'], note=b.get('note'), actor_id=user.id)
        audit('create', 'career_event', actor_id=user.id, row_id=ev['id'],
              after={'employeeId': id_, 'kind': b['kind'], 'effectiveDate': b['effectiveDate'], 'status': ev['status']})
    return ev


def cancel_career(user: AuthUser, id_: str, event_id: str) -> None:
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    event_id = parse_uuid(str(event_id))
    with transaction.atomic():
        career_mod.cancel_career_event(company_id, id_, event_id)
        audit('cancel', 'career_event', actor_id=user.id, row_id=event_id)
