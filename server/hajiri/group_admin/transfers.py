"""
Transfers between sister companies (/group/transfers), for group HR and group admins.

An employee of the ACTIVE company moves to another company of the group from `effectiveDate` (their first day
there). In one transaction:

1. Source: the employment ends the day before through the existing exit flow (hajiri.lifecycle.service.record_exit:
   an accepted exit, the exit date set, the offboarding checklist with its "final settlement in payroll" task);
   the exit record says it is a transfer, and a 'transfer' career event is logged.
2. Target: a new employee record with the person's details (PAN and bank account re-encrypted for the new row),
   joining on the effective date (payroll prorates from it), confirmation date kept, the career history copied
   (so the original hire date, the service start, stays on the timeline) and a 'transfer' career event.
3. Leave balances carry over (by leave type code) only when asked AND the target company's transfer rule allows
   it; the source balance is reduced by the same days, so it is neither encashed nor counted twice.
4. Salary carries over when asked and the target has a salary structure with the same code.
5. The person's account follows: their default (home) access and employee link move to the target; they keep
   employee access to the source with the old record until it ends (a membership whose employee has left no
   longer counts, hajiri.core.auth.company_access).

Audit entries on both sides ('transfer_out' on the source record, 'transfer_in' on the new one, plus the account
changes) and notifications to the person and the target's HR.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.backends.postgresql.psycopg_any import DateRange
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce, Now

from hajiri.core import settings_store
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, ROLES, company_id_of, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import npt_today
from hajiri.core.collation import collate_key
from hajiri.core.crypto import field_cipher
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.scoping import as_uuid
from hajiri.core.validation import plain, validate
from hajiri.db.models import (AppUser, AuditLog, Branch, CareerEvent, CompanyMembership, CompanySetting, Employee, EmployeeAssignment,
                              EmployeeExit, LeaveLedger, LeaveType, OrgUnit, SalaryAssignment, SalaryStructureVersion,
                              Shift, ShiftAssignment)
from hajiri.notifications.service import notify, notify_role

from .common import group_scope
from .serializers import TransferIn, TransferRuleIn

RULE_KEY = 'group.transfer'
REF_TYPE = 'group_transfer'
BACKDATE_DAYS = 31
AHEAD_DAYS = 365
# The exit record's kind (employee_exit.kind accepts 'transfer' since migration 0005).
EXIT_KIND = EmployeeExit.Kind.TRANSFER
REASON_CATEGORY = 'Transfer'

# Personal and statutory details that follow the person to the new record.
COPY_FIELDS = (
    'name_en', 'name_ne', 'gender', 'dob', 'marital_status', 'grade', 'phone', 'email', 'ot_eligible', 'weekly_off_days',
    'regime', 'ssf_id', 'pf_id', 'cit_id', 'pan_bidx', 'bank_code', 'bank_branch', 'bank_account_last4', 'is_resident',
    'remote_area_class', 'disability', 'festival_month', 'work_from_home', 'blood_group', 'nationality', 'personal_email',
    'personal_phone', 'work_phone', 'permanent_address', 'temporary_address', 'skills', 'languages',
    'citizenship_district', 'citizenship_issued_on', 'passport_expiry', 'licence_expiry', 'confirmed_on', 'field_work',
    'contract_end',
)
# Encrypted columns: the AAD names the row (employee:<id>:<purpose>), so they are decrypted and encrypted again.
SECRET_FIELDS = (('pan_enc', 'pan'), ('bank_account_enc', 'bank'), ('citizenship_no_enc', 'citizenship'),
                 ('passport_no_enc', 'passport'), ('licence_no_enc', 'licence'))
# Career event details that make sense in another company (ids of branches, units and people do not).
PORTABLE_DETAILS = ('designation', 'grade', 'employmentType', 'separationType', 'revisionPercent', 'reason', 'endDate')


# ---------------------------------------------------------------- the target company's rule

def carries_leave(company_id) -> bool:
    return bool((settings_store.get_setting(company_id, RULE_KEY) or {}).get('carryLeaveBalance', False))


def transfer_rules(actor: AuthUser) -> list[dict]:
    """Each company's transfer rule (whether leave balances of people transferred in carry over)."""
    scope = group_scope(actor, hr=True)
    stored = dict(CompanySetting.objects.filter(company_id__in=scope.ids, key=RULE_KEY)
                  .values_list('company_id', 'value'))
    return [{'companyId': str(c.pk), 'name': c.name_en,
             'carryLeaveBalance': bool((stored.get(c.pk) or {}).get('carryLeaveBalance', False))} for c in scope.sorted()]


@transaction.atomic
def set_transfer_rule(actor: AuthUser, body: dict) -> dict:
    """Group admins decide per company whether people transferred in keep their leave balance."""
    scope = group_scope(actor)
    b = plain(validate(TransferRuleIn, body))
    c = scope.company(b['companyId'])
    before = settings_store.get_setting(c.pk, RULE_KEY)
    value = {**(before or {}), 'carryLeaveBalance': b['carryLeaveBalance']}
    settings_store.put_setting(c.pk, RULE_KEY, value, updated_by=actor.id)
    audit('update', 'company_setting', actor_id=actor.id, row_id=f'{c.pk}:{RULE_KEY}', before=before, after=value)
    return {'companyId': str(c.pk), **value}


# ---------------------------------------------------------------- what the form offers

def _require_hr_in(actor: AuthUser, company_id) -> None:
    ref = actor.company(company_id)
    if ref is None or not ref.has_role('hr'):
        raise ForbiddenError('requires role hr')


def target_companies(actor: AuthUser) -> list[dict]:
    """Sister companies the active company's employees can move to (where the actor is HR)."""
    scope = group_scope(actor, hr=True)
    return [{'id': str(c.pk), 'name': c.name_en, 'code': c.code, 'carryLeaveBalance': carries_leave(c.pk)}
            for c in scope.sorted() if str(c.pk) != actor.company_id and _hr_in(actor, c.pk)]


def _hr_in(actor: AuthUser, company_id) -> bool:
    ref = actor.company(company_id)
    return bool(ref and ref.has_role('hr'))


def target_options(actor: AuthUser, company_id) -> dict:
    """Branches, departments, managers and shifts of a target company, for the transfer form."""
    scope = group_scope(actor, hr=True)
    c = scope.company(company_id)
    if str(c.pk) == actor.company_id:
        raise BadRequestError('choose a sister company other than the one the employee works in')
    _require_hr_in(actor, c.pk)

    def opts(qs, label='name_en'):
        rows = [{'id': str(r['id']), 'name': r[label], 'code': r.get('code') or r.get('emp_code')} for r in qs]
        return sorted(rows, key=lambda r: collate_key(r['name']))
    return {'company': {'id': str(c.pk), 'name': c.name_en, 'carryLeaveBalance': carries_leave(c.pk)},
            'branches': opts(Branch.objects.for_company(c.pk).values('id', 'name_en', 'code')),
            'orgUnits': opts(OrgUnit.objects.for_company(c.pk).values('id', 'name_en', 'code')),
            'managers': opts(Employee.objects.for_company(c.pk).filter(status=Employee.Status.ACTIVE)
                             .values('id', 'name_en', 'emp_code')),
            'shifts': opts(Shift.objects.for_company(c.pk).values('id', 'name_en', 'code'))}


# ---------------------------------------------------------------- the transfer

def _ref(model_or_qs, company_id, value, message):
    """An id from the form that must be a row of the TARGET company."""
    if not value:
        return None
    qs = model_or_qs.objects.all() if isinstance(model_or_qs, type) else model_or_qs
    if not qs.for_company(company_id).filter(pk=as_uuid(value)).exists():
        raise NotFoundError(message)
    return str(value)


def _account(src: Employee):
    """The account that works as this employee: ('home', AppUser) or ('membership', CompanyMembership)."""
    u = AppUser.objects.select_for_update().filter(employee_id=src.pk).first()
    if u:
        return 'home', u
    ms = CompanyMembership.objects.select_for_update().filter(employee_id=src.pk).select_related('user').first()
    return ('membership', ms) if ms else (None, None)


def _check_account_free(kind, acc, target_id: str, target_name: str) -> None:
    """A person has at most one employee record per company."""
    if kind is None:
        return
    uid = acc.pk if kind == 'home' else acc.user_id
    linked_home = AppUser.objects.filter(pk=uid, company_id=target_id, employee__isnull=False,
                                         employee__status=Employee.Status.ACTIVE).exists()
    linked_member = CompanyMembership.objects.filter(user_id=uid, company_id=target_id, employee__isnull=False).exists()
    if linked_home or linked_member:
        raise ConflictError(f'this person already has an employee record in {target_name}')


def _copy_secrets(src: Employee, new_id: str) -> dict:
    c = field_cipher()
    out = {}
    for col, purpose in SECRET_FIELDS:
        blob = getattr(src, col)
        if blob:
            out[col] = c.encrypt(c.decrypt(bytes(blob), f'employee:{src.pk}:{purpose}'), f'employee:{new_id}:{purpose}')
    return out


def _copy_history(src: Employee, new_id: str, source_name: str, target_id: str) -> int:
    """The source's applied career events, re-created on the new record (ids of the old company's branches and
    people left out), so the timeline starts at the original hire."""
    events = (CareerEvent.objects.for_company(src.company_id).filter(employee=src, status=CareerEvent.Status.APPLIED)
              .order_by('effective_date', 'created_at', 'id'))
    rows = [CareerEvent(company_id=target_id, employee_id=new_id, kind=e.kind, effective_date=e.effective_date,
                        details={k: v for k, v in (e.details or {}).items() if k in PORTABLE_DETAILS},
                        note=f'At {source_name}' + (f': {e.note}' if e.note else ''), status=CareerEvent.Status.APPLIED,
                        applied_at=e.applied_at, created_by_id=e.created_by_id) for e in events]
    CareerEvent.objects.bulk_create(rows)
    return len(rows)


def _carry_leave(src: Employee, new_id: str, target_id: str, eff: date, lwd: date, actor: AuthUser,
                 target_name: str, source_name: str) -> list[dict]:
    """Positive balances of leave types the target also has (same code) move with the person."""
    balances = (LeaveLedger.objects.filter(employee=src).values('leave_type_id', 'leave_type__code', 'leave_type__name_en')
                .annotate(days=Sum('delta_days')).filter(days__gt=0).order_by('leave_type__sort_order'))
    by_code = {r['leave_type__code']: r for r in balances}
    targets = dict(LeaveType.objects.for_company(target_id).filter(code__in=list(by_code)).values_list('code', 'id'))
    carried, rows = [], []
    for code, r in by_code.items():
        if code not in targets:
            continue
        days = Decimal(r['days'])
        rows += [LeaveLedger(employee_id=src.pk, leave_type_id=r['leave_type_id'], effective_date=lwd, delta_days=-days,
                             reason=LeaveLedger.Reason.ADJUSTMENT, ref_type=REF_TYPE, ref_id=new_id,
                             note=f'Carried over to {target_name} on transfer', posted_by_id=actor.id),
                 LeaveLedger(employee_id=new_id, leave_type_id=targets[code], effective_date=eff, delta_days=days,
                             reason=LeaveLedger.Reason.CARRY_FORWARD, ref_type=REF_TYPE, ref_id=src.pk,
                             note=f'Carried over from {source_name} on transfer', posted_by_id=actor.id)]
        carried.append({'code': code, 'name': r['leave_type__name_en'], 'days': str(days.normalize())})
    LeaveLedger.objects.bulk_create(rows)
    return carried


def _carry_salary(src: Employee, new_id: str, target_id: str, eff: date, lwd: date, actor: AuthUser) -> str | None:
    """The salary assignment on the last working day, on the target's structure with the same code (latest
    version). Returns why it could not be carried, or None."""
    cur = (SalaryAssignment.objects.filter(employee=src, valid__contains=lwd)
           .select_related('structure_version__structure').first())
    if cur is None:
        return 'there is no salary assignment on the last working day'
    code = cur.structure_version.structure.code
    sv = (SalaryStructureVersion.objects.for_company(target_id).filter(structure__code=code)
          .order_by('-version').first())
    if sv is None:
        return f'the target company has no salary structure {code}'
    in_structure = {c['code'] for c in sv.components}
    amounts = {k: v for k, v in cur.base_amounts.items() if k in in_structure}
    if not amounts:
        return f'salary structure {code} of the target company has none of the same components'
    SalaryAssignment.objects.create(employee_id=new_id, structure_version=sv, valid=DateRange(eff, None),
                                    base_amounts=amounts, created_by_id=actor.id)
    SalaryStructureVersion.objects.filter(pk=sv.pk).update(frozen_at=Coalesce('frozen_at', Now()))
    audit('create', 'salary_assignment', actor_id=actor.id, row_id=new_id,
          after={'structureVersionId': str(sv.pk), 'from': eff.isoformat(), 'baseAmounts': '***'})
    return None


def _move_account(kind, acc, src: Employee, new_id: str, source_id: str, target_id: str, actor: AuthUser) -> str | None:
    """The person's default access and employee link follow them to the target company."""
    if kind is None:
        return None
    if kind == 'home':
        u: AppUser = acc
        before = {'companyId': str(u.company_id), 'employeeId': str(u.employee_id), 'roles': list(u.roles)}
        there = CompanyMembership.objects.filter(user_id=u.pk, company_id=target_id).first()
        roles = list(dict.fromkeys([*(there.roles if there else []), AppUser.Role.EMPLOYEE]))
        roles = [r for r in ROLES if r in roles]
        if there:
            CompanyMembership.objects.filter(user_id=u.pk, company_id=target_id).delete()
        # A fresh sign-in starts in the new home company.
        CompanyMembership.objects.filter(user_id=u.pk, is_default=True).update(is_default=False)
        AppUser.objects.filter(pk=u.pk).update(company_id=target_id, employee_id=new_id, roles=roles)
        # Self-service in the old company (payslips, the exit checklist) until the old record ends.
        CompanyMembership.objects.update_or_create(
            user_id=u.pk, company_id=source_id,
            defaults={'roles': before['roles'], 'employee_id': src.pk, 'status': CompanyMembership.Status.ACTIVE},
            create_defaults={'roles': before['roles'], 'employee_id': src.pk, 'created_by_id': actor.id})
        audit('update', 'app_user', actor_id=actor.id, row_id=str(u.pk), before=before,
              after={'companyId': target_id, 'employeeId': new_id, 'roles': roles})
        audit('create', 'company_membership', actor_id=actor.id, row_id=f'{u.pk}:{source_id}',
              after={'roles': before['roles'], 'employeeId': str(src.pk), 'reason': 'transfer'})
        return str(u.pk)
    ms: CompanyMembership = acc
    uid = ms.user_id
    home = AppUser.objects.select_for_update().get(pk=uid)
    was_default = ms.is_default
    if home.company_id and str(home.company_id) == target_id:
        # Back to the home company: the new record becomes the home employee.
        before = {'employeeId': home.employee_id and str(home.employee_id)}
        AppUser.objects.filter(pk=uid).update(employee_id=new_id)
        CompanyMembership.objects.filter(user_id=uid, is_default=True).update(is_default=False)
        audit('update', 'app_user', actor_id=actor.id, row_id=str(uid), before=before, after={'employeeId': new_id})
        return str(uid)
    there = CompanyMembership.objects.select_for_update().filter(user_id=uid, company_id=target_id).first()
    roles = [r for r in ROLES if r in {*(there.roles if there else []), AppUser.Role.EMPLOYEE}]
    if was_default:
        CompanyMembership.objects.filter(user_id=uid, is_default=True).update(is_default=False)
    CompanyMembership.objects.update_or_create(
        user_id=uid, company_id=target_id,
        defaults={'roles': roles, 'employee_id': new_id, 'status': CompanyMembership.Status.ACTIVE,
                  **({'is_default': True} if was_default else {})},
        create_defaults={'roles': roles, 'employee_id': new_id, 'created_by_id': actor.id, 'is_default': was_default})
    audit('update' if there else 'create', 'company_membership', actor_id=actor.id, row_id=f'{uid}:{target_id}',
          **({'before': {'roles': list(there.roles)}} if there else {}),
          after={'roles': roles, 'employeeId': new_id, 'isDefault': was_default, 'reason': 'transfer'})
    return str(uid)


def transfer_employee(actor: AuthUser, body: dict) -> dict:
    """Transfer an employee of the active company to a sister company (see the module docstring)."""
    from hajiri.lifecycle.service import record_exit

    scope = group_scope(actor, hr=True)
    source_id = company_id_of(actor)
    if not has_role(actor, 'hr'):
        raise ForbiddenError('requires role hr')
    b = plain(validate(TransferIn, body))
    target = scope.company(b['toCompanyId'])
    target_id = str(target.pk)
    if target_id == source_id:
        raise BadRequestError('choose a sister company other than the one the employee works in')
    _require_hr_in(actor, target_id)
    source = scope.company(source_id)
    today = npt_today()
    eff = date.fromisoformat(b['effectiveDate'])
    lwd = eff - timedelta(days=1)
    if eff < today - timedelta(days=BACKDATE_DAYS):
        raise BadRequestError(f'the transfer date cannot be more than {BACKDATE_DAYS} days in the past')
    if eff > today + timedelta(days=AHEAD_DAYS):
        raise BadRequestError('the transfer date must be within a year')
    if b['carryLeave'] and not carries_leave(target_id):
        raise BadRequestError(f'{target.name_en} does not carry leave balances over on transfer')

    with transaction.atomic():
        src = Employee.objects.for_company(source_id).select_for_update().filter(pk=as_uuid(b['employeeId'])).first()
        if src is None:
            raise NotFoundError('employee not found')
        if str(src.pk) == actor.employee_id:
            raise ForbiddenError('another HR user must record your own transfer')
        if src.status != Employee.Status.ACTIVE or (src.exit_date and src.exit_date < today):
            raise BadRequestError('this employee has already left')
        if EmployeeExit.objects.filter(employee=src, status__in=(EmployeeExit.Status.PENDING, EmployeeExit.Status.ACCEPTED)).exists():
            raise ConflictError('an exit is already in progress for this employee')
        if src.join_date and lwd < src.join_date:
            raise BadRequestError('the transfer date must be after the employee joined')
        code = (b.get('empCode') or '').strip() or src.emp_code
        if Employee.objects.for_company(target_id).filter(emp_code=code).exists():
            raise ConflictError(f'employee code {code} is already used in {target.name_en}')
        branch = _ref(Branch, target_id, b.get('branchId'), 'branch not found')
        unit = _ref(OrgUnit, target_id, b.get('orgUnitId'), 'department not found')
        manager = _ref(Employee.objects.filter(status=Employee.Status.ACTIVE), target_id, b.get('managerId'), 'manager not found')
        shift = _ref(Shift, target_id, b.get('shiftId'), 'shift not found')
        kind, acc = _account(src)
        _check_account_free(kind, acc, target_id, target.name_en)
        current = (EmployeeAssignment.objects.filter(employee=src, valid__contains=min(lwd, today))
                   .values('employment_type').first())

        new = Employee.objects.create(company_id=target_id, emp_code=code, join_date=eff,
                                      designation=b.get('designation') or src.designation,
                                      probation_end=None, **{f: getattr(src, f) for f in COPY_FIELDS})
        new_id = str(new.pk)
        secrets = _copy_secrets(src, new_id)
        if secrets:
            Employee.objects.filter(pk=new_id).update(**secrets)
        EmployeeAssignment.objects.create(
            employee_id=new_id, valid=DateRange(eff, None), branch_id=branch, org_unit_id=unit, manager_id=manager,
            employment_type=(current or {}).get('employment_type') or EmployeeAssignment.EmploymentType.REGULAR)
        if shift:
            ShiftAssignment.objects.create(employee_id=new_id, shift_id=shift, valid=DateRange(eff, None))
        copied = _copy_history(src, new_id, source.name_en, target_id)
        details = {**({'branchId': branch} if branch else {}), **({'orgUnitId': unit} if unit else {}),
                   **({'managerId': manager} if manager else {})}
        CareerEvent.objects.create(company_id=target_id, employee_id=new_id, kind=CareerEvent.Kind.TRANSFER,
                                   effective_date=eff, details=details, status=CareerEvent.Status.APPLIED, applied_at=Now(),
                                   note=f'Transferred from {source.name_en} (employee code {src.emp_code})'
                                        + (f'. {b["note"]}' if b.get('note') else ''),
                                   created_by_id=actor.id)

        exit_id = record_exit(company_id=source_id, employee_id=str(src.pk), kind=EXIT_KIND, voluntary=True,
                              reason_category=REASON_CATEGORY, reason=f'Transfer to {target.name_en}'
                              + (f'. {b["note"]}' if b.get('note') else ''), last_working_day=lwd, actor_id=actor.id)
        CareerEvent.objects.create(company_id=source_id, employee=src, kind=CareerEvent.Kind.TRANSFER, effective_date=eff,
                                   details={}, status=CareerEvent.Status.APPLIED, applied_at=Now(),
                                   note=f'Transferred to {target.name_en} (employee code {code})', created_by_id=actor.id)

        leave = _carry_leave(src, new_id, target_id, eff, lwd, actor, target.name_en, source.name_en) if b['carryLeave'] else []
        salary_note = _carry_salary(src, new_id, target_id, eff, lwd, actor) if b['carrySalary'] else None
        user_id = _move_account(kind, acc, src, new_id, source_id, target_id, actor)

        summary = {'fromCompanyId': source_id, 'fromCompany': source.name_en, 'fromEmployeeId': str(src.pk),
                   'fromEmpCode': src.emp_code, 'toCompanyId': target_id, 'toCompany': target.name_en,
                   'toEmployeeId': new_id, 'toEmpCode': code, 'name': src.name_en, 'effectiveDate': eff.isoformat(),
                   'lastWorkingDay': lwd.isoformat(), 'serviceStart': src.join_date and src.join_date.isoformat(),
                   'exitId': exit_id, 'leaveCarried': leave, 'salaryCarried': b['carrySalary'] and salary_note is None,
                   'historyCopied': copied, 'userId': user_id}
        audit('create', 'employee', actor_id=actor.id, row_id=new_id,
              after={'empCode': code, 'nameEn': src.name_en, 'joinDate': eff.isoformat(), 'transferFrom': source_id})
        audit('transfer_out', 'employee', actor_id=actor.id, row_id=str(src.pk), after=summary)
        audit('transfer_in', 'employee', actor_id=actor.id, row_id=new_id, after=summary)

        if user_id:
            notify(company_id=target_id, user_id=user_id, employee_id=new_id, template='group.transfer',
                   title=f'Welcome to {target.name_en}',
                   body=f'From {bs_label(eff)} you work at {target.name_en}. Your records at {source.name_en} stay '
                        f'available until your last working day there, {bs_label(lwd)}.',
                   payload={'employeeId': new_id}, channels=('push',))
        notify_role(target_id, 'hr', template='group.transfer_in', title=f'{src.name_en} transfers in',
                    body=f'{src.name_en} joins {target.name_en} from {source.name_en} on {bs_label(eff)}. '
                         'Check their shift, salary and leave.',
                    payload={'employeeId': new_id}, employee_id=new_id)
    return {**summary, 'salaryNote': salary_note}


# ---------------------------------------------------------------- history

def list_transfers(actor: AuthUser, limit: int = 100) -> list[dict]:
    """Recent transfers into or out of the companies of the group where the actor is HR."""
    scope = group_scope(actor, hr=True)
    ids = [cid for cid in scope.ids if _hr_in(actor, cid)]
    if not ids:
        return []
    rows = list(AuditLog.objects.filter(action='transfer_in', table_name='employee')
                .filter(Q(after__toCompanyId__in=ids) | Q(after__fromCompanyId__in=ids))
                .order_by('-occurred_at', '-id').values('occurred_at', 'actor_id', 'after')[:limit])
    names = dict(AppUser.objects.filter(pk__in={r['actor_id'] for r in rows if r['actor_id']}).values_list('id', 'name'))
    return [{**r['after'], 'recordedAt': r['occurred_at'], 'recordedBy': names.get(r['actor_id'])} for r in rows]
