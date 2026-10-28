"""
Employee lifecycle engine (port of apps/api/src/modules/lifecycle/service.ts): per-company defaults, onboarding and
exit checklists, probation decisions, exits and the daily job.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import (BooleanField, Count, DecimalField, Exists, ExpressionWrapper, F, OuterRef, Q, Subquery, Sum,
                              Value)
from django.db.models.functions import Coalesce, Now

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, ConflictError, NotFoundError, ValidationError
from hajiri.core.settings_store import add_setting_if_missing, get_setting
from hajiri.core.users import user_ids_with_role
from hajiri.core.validation import validate
from hajiri.db.models import (AssetAssignment, AssetType, Company, Employee, EmployeeAssignment, EmployeeConfirmation,
                              EmployeeExit, ExitInterviewQuestion, LeaveLedger, LifecycleCase, LifecycleReminder, LifecycleTask,
                              LifecycleTemplate, LifecycleTemplateTask, Loan, LoanInstallment, PayrollPeriod, PayrollRun,
                              ProbationReview)
from hajiri.notifications.service import notify
from hajiri.people.accounts import user_id_for_employee
from hajiri.people.career import log_career_event

from .serializers import EXIT_KINDS, PolicyIn  # noqa: F401

CaseStatus = LifecycleCase.Status
TaskStatus = LifecycleTask.Status
ExitStatus = EmployeeExit.Status
POLICY_KEY = 'lifecycle.policy'
TASK_COLS = ('id', 'case_id', 'company_id', 'employee_id', 'sort', 'title', 'description', 'department', 'assignee_type',
             'assignee_user_id', 'due_date', 'link', 'status', 'note', 'done_by', 'done_at', 'created_at')


# ---------------------------------------------------------------- policy

def lifecycle_policy(company_id: str) -> dict:
    """The company's lifecycle settings; a stored value that no longer validates falls back to the defaults."""
    try:
        return validate(PolicyIn, get_setting(company_id, POLICY_KEY) or {})
    except ValidationError:
        return validate(PolicyIn, {})


# ---------------------------------------------------------------- defaults
ONBOARDING = [
    ('Prepare the appointment letter and contract', 'hr', -3, 'HR', None, None),
    ('Set up the workstation, laptop and email', 'manager', -1, 'IT', None, 'Ask IT for a laptop, an email address and access to the tools the role needs.'),
    ('Create the Hajiri sign-in and assign a shift', 'hr', 0, 'HR', '/settings/users', None),
    ('Enrol fingerprint or face on the attendance terminal', 'hr', 0, 'HR', None, None),
    ('Complete your profile and bank details', 'employee', 0, None, '/me/profile', 'Check your contact details, emergency contact and bank account.'),
    ('Upload your citizenship, PAN and academic certificates', 'employee', 1, None, '/me/profile/documents', None),
    ('Assign the salary structure and SSF registration', 'hr', 2, 'HR', '/payroll/assignments', None),
    ('Welcome meeting: introduce the team and the role', 'manager', 1, None, None, None),
    ('Read the employee handbook and leave policy', 'employee', 7, None, None, None),
    ('Agree goals for the probation period', 'manager', 14, None, '/me/growth', None),
    ('First-month check-in', 'manager', 30, None, None, None),
]

OFFBOARDING = [
    ('Hand over work, files and passwords', 'manager', -3, 'Manager', None, None),
    ('Exit interview', 'hr', -2, 'HR', None, None),
    ('Return laptop and devices; remove email and system access', 'hr', 0, 'IT', None, None),
    ('Clear advances, loans and other dues', 'hr', 0, 'Finance', '/payroll/loans', None),
    ('Return ID card, keys and SIM', 'hr', 0, 'Admin', None, None),
    ('Run the final settlement in payroll', 'hr', 3, 'HR', '/payroll', None),
    ('Issue the experience and relieving letter', 'hr', 5, 'HR', None, None),
]

EXIT_QUESTIONS = [
    ('What is the main reason you are leaving?', 'choice', ['Better pay', 'Career growth', 'Work-life balance', 'Manager or team', 'Relocation',
                                                            'Further studies', 'Health or family', 'Other']),
    ('How would you rate your overall experience here?', 'rating', []),
    ('How would you rate the support from your manager?', 'rating', []),
    ('What did you like most about working here?', 'text', []),
    ('What should we improve?', 'text', []),
    ('Would you recommend us as a place to work?', 'choice', ['Yes', 'Maybe', 'No']),
]

ASSET_TYPES = ['Laptop', 'Desktop', 'Mobile phone', 'SIM card', 'ID card', 'Keys', 'Vehicle', 'Other']


def ensure_lifecycle_defaults(company_id: str) -> bool:
    """Idempotent per-company defaults; the marker setting makes concurrent first visits seed only once."""
    if not add_setting_if_missing(company_id, 'lifecycle.seeded', {'version': 1}):
        return False
    for kind, name, tasks in ((LifecycleCase.Kind.ONBOARDING, 'Standard onboarding', ONBOARDING),
                              (LifecycleCase.Kind.OFFBOARDING, 'Standard exit clearance', OFFBOARDING)):
        t, created = LifecycleTemplate.objects.get_or_create(
            company_id=company_id, kind=kind, name=name,
            defaults={'is_default': True, 'description': 'Checklist for every new joiner. Due dates count from the join date.'
                      if kind == 'onboarding' else 'Clearance for every leaver. Due dates count from the last working day.'})
        if not created:
            continue
        LifecycleTemplateTask.objects.bulk_create([
            LifecycleTemplateTask(template=t, sort=i, title=title, description=description, department=dept, assignee_type=assignee,
                                  due_offset_days=offset, link=link)
            for i, (title, assignee, offset, dept, link, description) in enumerate(tasks)])
    ExitInterviewQuestion.objects.bulk_create([
        ExitInterviewQuestion(company_id=company_id, sort=i, question=q, kind=kind, options=options)
        for i, (q, kind, options) in enumerate(EXIT_QUESTIONS)])
    AssetType.objects.bulk_create([AssetType(company_id=company_id, name=n) for n in ASSET_TYPES], ignore_conflicts=True)
    return True


# ---------------------------------------------------------------- reminders
def remind_once(company_id: str, kind: str, ref_id: str, key: str) -> bool:
    """Record a reminder; False when it was already sent (callers skip the notification)."""
    _, created = LifecycleReminder.objects.get_or_create(company_id=company_id, kind=kind, ref_id=ref_id, key=key)
    return created


# ---------------------------------------------------------------- checklists
def task_row(r: dict | None) -> dict | None:
    if r is None:
        return None
    out = dict(r)
    for k in ('id', 'case_id', 'company_id', 'employee_id', 'assignee_user_id', 'done_by'):
        if out.get(k) is not None:
            out[k] = str(out[k])
    return out


def _now_or_later(today: date) -> Q:
    """Assignment periods that cover today or start later (pre-joining onboarding)."""
    return Q(valid__contains=today) | Q(valid__startswith__gt=today)


def manager_user_of(employee_id: str) -> str | None:
    """The account of the employee's line manager now (or the first one they will have), if that manager can sign in."""
    managers = (EmployeeAssignment.objects.filter(employee_id=employee_id, manager__isnull=False)
                .filter(_now_or_later(npt_today())).order_by('valid__startswith').values_list('manager_id', flat=True))
    for m in managers:
        uid = user_id_for_employee(m)
        if uid:
            return uid
    return None


def responsible_users(t: dict) -> list[str]:
    """Users who should act on a task: HR, the line manager (HR when there is none), the employee, or the named user."""
    kind = t['assignee_type']
    if kind == 'hr':
        return user_ids_with_role(t['company_id'], 'hr')
    if kind == 'manager':
        m = manager_user_of(t['employee_id'])
        return [m] if m else user_ids_with_role(t['company_id'], 'hr')
    if kind == 'employee':
        u = user_id_for_employee(t['employee_id'])
        return [u] if u else []
    return [str(t['assignee_user_id'])] if t.get('assignee_user_id') else user_ids_with_role(t['company_id'], 'hr')


def manages_now_or_soon(manager_employee_id: str, employee_id: str) -> bool:
    """The employee's current line manager, or the manager they will have once they join (pre-joining onboarding)."""
    return (EmployeeAssignment.objects.filter(employee_id=employee_id, manager_id=manager_employee_id)
            .filter(_now_or_later(npt_today())).exists())


def can_complete_task(user: AuthUser, t: dict) -> bool:
    """HR may complete anything; otherwise only the person the task is for."""
    if user.company_id != str(t['company_id']):
        return False
    if has_role(user, 'hr'):
        return True
    kind = t['assignee_type']
    if kind == 'hr':
        return False
    if kind == 'employee':
        return user.employee_id == str(t['employee_id'])
    if kind == 'manager':
        return bool(user.employee_id) and manages_now_or_soon(user.employee_id, str(t['employee_id']))
    return str(t.get('assignee_user_id')) == user.id


def can_view_case(user: AuthUser, c: dict) -> bool:
    """HR, the employee, their line manager, and anyone with a task in the case may see it."""
    if user.company_id != str(c['company_id']):
        return False
    if has_role(user, 'hr'):
        return True
    if user.employee_id == str(c['employee_id']):
        return True
    if user.employee_id and has_role(user, 'manager') and manages_now_or_soon(user.employee_id, str(c['employee_id'])):
        return True
    return LifecycleTask.objects.filter(case_id=c['id'], assignee_type=LifecycleTask.AssigneeType.USER, assignee_user_id=user.id).exists()


def _d(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _flag(q: Q) -> ExpressionWrapper:
    return ExpressionWrapper(q, output_field=BooleanField())


def start_case(*, company_id: str, employee_id: str, kind: str, anchor_date, started_by: str | None,
               template_id: str | None = None, exit_id: str | None = None) -> str:
    """
    Open a checklist for an employee from a template (the best match for their branch and department when none is
    given). Due dates count from the anchor date: the join date for onboarding, the last working day for exits.
    """
    ensure_lifecycle_defaults(company_id)
    anchor = _d(anchor_date)
    templates = LifecycleTemplate.objects.for_company(company_id).filter(kind=kind, active=True)
    if template_id:
        if not templates.filter(pk=template_id).exists():
            raise BadRequestError('checklist template not found')
    else:
        a = (EmployeeAssignment.objects.filter(employee_id=employee_id, valid__contains=max(npt_today(), anchor))
             .values('branch_id', 'org_unit_id').first()) or {}
        best = (templates.filter(Q(branch__isnull=True) | Q(branch_id=a.get('branch_id')) if a.get('branch_id') else Q(branch__isnull=True))
                .filter(Q(org_unit__isnull=True) | Q(org_unit_id=a.get('org_unit_id')) if a.get('org_unit_id') else Q(org_unit__isnull=True))
                .order_by(_flag(Q(org_unit__isnull=False)).desc(), _flag(Q(branch__isnull=False)).desc(), '-is_default', 'created_at')
                .values_list('id', flat=True).first())
        template_id = str(best) if best else None
    if LifecycleCase.objects.filter(employee_id=employee_id, kind=kind, status=CaseStatus.ACTIVE).exists():
        raise ConflictError('onboarding is already in progress for this employee' if kind == 'onboarding'
                            else 'an exit clearance is already in progress for this employee')
    case = LifecycleCase.objects.create(company_id=company_id, employee_id=employee_id, kind=kind, template_id=template_id,
                                        anchor_date=anchor, started_by_id=started_by, exit_id=exit_id)
    case_id = str(case.pk)
    if template_id:
        LifecycleTask.objects.bulk_create([
            LifecycleTask(case=case, company_id=company_id, employee_id=employee_id, sort=x.sort, title=x.title, description=x.description,
                          department=x.department, assignee_type=x.assignee_type, assignee_user_id=x.assignee_user_id,
                          due_date=anchor + timedelta(days=x.due_offset_days), link=x.link)
            for x in LifecycleTemplateTask.objects.filter(template_id=template_id).order_by('sort')])
    audit('start', 'lifecycle_case', actor_id=started_by, row_id=case_id,
          after={'kind': kind, 'employeeId': str(employee_id), 'templateId': template_id})
    name = Employee.objects.filter(pk=employee_id).values_list('name_en', flat=True).first() or 'An employee'
    # Tell each person once that they have something to do, with their own count.
    per_user: dict[str, int] = {}
    resolved: dict[tuple, list[str]] = {}   # every task of a case shares the employee: resolve each assignee kind once
    for t in LifecycleTask.objects.filter(case_id=case_id).values(*TASK_COLS):
        key = (t['assignee_type'], t['assignee_user_id'])
        if key not in resolved:
            resolved[key] = responsible_users(task_row(t))
        for u in resolved[key]:
            per_user[u] = per_user.get(u, 0) + 1
    own_user = user_id_for_employee(str(employee_id))
    for user_id, n in per_user.items():
        if user_id == started_by:
            continue
        own = own_user == user_id
        if kind == 'onboarding':
            title = 'Welcome! Your getting-started checklist' if own else f'Onboarding for {name}'
        else:
            title = 'Your exit checklist' if own else f'Exit clearance for {name}'
        notify(company_id=company_id, user_id=user_id, template=f'lifecycle.{kind}.started', title=title,
               body=f"{n} {'task is' if n == 1 else 'tasks are'} waiting for you.", payload={'caseId': case_id})
    return case_id


def complete_case_if_done(case_id: str) -> bool:
    """Mark a case completed once nothing is left open."""
    open_tasks = LifecycleTask.objects.filter(case_id=OuterRef('pk'), status=TaskStatus.OPEN)
    return (LifecycleCase.objects.filter(pk=case_id, status=CaseStatus.ACTIVE).exclude(Exists(open_tasks))
            .update(status=CaseStatus.COMPLETED, completed_at=Now())) > 0


# ---------------------------------------------------------------- probation
def apply_confirmation(*, company_id: str, employee_id: str, confirmed_on, review_id: str | None, actor_id: str | None) -> str:
    """The confirmation effect of an approved probation review (or a direct HR confirmation): stamps the employee as
    confirmed, keeps a confirmation record and logs it on the career timeline."""
    Employee.objects.filter(pk=employee_id).update(confirmed_on=confirmed_on, updated_at=Now())
    r = EmployeeConfirmation.objects.create(company_id=company_id, employee_id=employee_id, confirmed_on=confirmed_on,
                                            review_id=review_id, created_by_id=actor_id)
    on = confirmed_on.isoformat() if isinstance(confirmed_on, date) else str(confirmed_on)
    audit('confirm', 'employee', actor_id=actor_id, row_id=employee_id, after={'confirmedOn': on, 'reviewId': review_id})
    log_career_event(company_id=company_id, employee_id=employee_id, kind='confirmation', effective_date=on,
                     note='Confirmed after the probation review' if review_id else None, actor_id=actor_id)
    return str(r.pk)


def _iso(d) -> str | None:
    if d is None:
        return None
    return d.isoformat() if isinstance(d, date) else str(d)


def apply_probation_decision(review_id: str, state: str, ctx) -> None:
    rv = ProbationReview.objects.select_for_update().filter(pk=review_id).first()
    if not rv:
        raise NotFoundError('probation review not found')
    if rv.status != ProbationReview.Status.PENDING:
        raise ConflictError(f'review already {rv.status}')
    company_id, employee_id = str(rv.company_id), str(rv.employee_id)
    ProbationReview.objects.filter(pk=review_id).update(status=state, decided_at=Now())
    name = Employee.objects.filter(pk=employee_id).values_list('name_en', flat=True).first() or 'the employee'
    outcome = 'was not approved'
    Rec = ProbationReview.Recommendation
    if state == 'approved':
        today = npt_today()
        own = user_id_for_employee(employee_id)
        if rv.recommendation == Rec.CONFIRM:
            on = rv.probation_end if rv.probation_end and rv.probation_end > today else today
            apply_confirmation(company_id=company_id, employee_id=employee_id, confirmed_on=on, review_id=str(review_id), actor_id=ctx.actor_id)
            outcome = f'confirmed from {bs_label(on)}'
            if own:
                notify(company_id=company_id, user_id=own, employee_id=employee_id, template='lifecycle.confirmed',
                       title='Congratulations, your employment is confirmed',
                       body=f'Your probation is complete. You are confirmed from {bs_label(on)}.', payload={'reviewId': str(review_id)},
                       channels=('push',))
        elif rv.recommendation == Rec.EXTEND:
            Employee.objects.filter(pk=employee_id).update(probation_end=rv.extend_to, updated_at=Now())
            audit('extend_probation', 'employee', actor_id=ctx.actor_id, row_id=employee_id,
                  before={'probationEnd': _iso(rv.probation_end)}, after={'probationEnd': _iso(rv.extend_to)})
            outcome = f'probation extended to {bs_label(rv.extend_to)}'
            if own:
                notify(company_id=company_id, user_id=own, employee_id=employee_id, template='lifecycle.probation_extended',
                       title='Probation extended',
                       body=f'Your probation now ends on {bs_label(rv.extend_to)}. Your manager will talk you through the next steps.',
                       payload={'reviewId': str(review_id)})
        else:
            record_exit(company_id=company_id, employee_id=employee_id, kind='termination', voluntary=False,
                        reason_category='Probation not successful', reason='Probation review', last_working_day=rv.last_working_day,
                        actor_id=ctx.actor_id)
            outcome = f'employment ends on {bs_label(rv.last_working_day)}'
    body = (f'Your review was approved: {outcome}.' if state == 'approved'
            else f"Your review was not approved: {ctx.reason or 'no reason given'}.")
    notify(company_id=company_id, user_id=str(rv.submitted_by_id), template=f'lifecycle.probation_review.{state}',
           title=f'Probation review for {name}', body=body, payload={'reviewId': str(review_id)})


# ---------------------------------------------------------------- exits
IN_PROGRESS = (ExitStatus.PENDING, ExitStatus.ACCEPTED)


def record_exit(*, company_id: str, employee_id: str, kind: str, voluntary: bool, last_working_day, actor_id: str | None,
                reason_category: str | None = None, reason: str | None = None, template_id: str | None = None) -> str:
    """
    An accepted exit: the last working day becomes the employee's exit date (sign-in stops the day after) and the exit
    clearance checklist starts. Used for HR-initiated exits and terminations after a failed probation.
    """
    if EmployeeExit.objects.filter(employee_id=employee_id, status__in=IN_PROGRESS).exists():
        raise ConflictError('an exit is already in progress for this employee')
    x = EmployeeExit.objects.create(company_id=company_id, employee_id=employee_id, kind=kind, voluntary=voluntary,
                                    reason_category=reason_category, reason=reason, submitted_on=npt_today(), notice_days=0,
                                    proposed_lwd=last_working_day, accepted_lwd=last_working_day, status=ExitStatus.ACCEPTED,
                                    initiated_by_id=actor_id, decided_at=Now())
    exit_id = str(x.pk)
    accept_exit(exit_id, last_working_day, actor_id, template_id)
    return exit_id


def accept_exit(exit_id: str, lwd, actor_id: str | None, template_id: str | None = None) -> None:
    EmployeeExit.objects.filter(pk=exit_id).update(status=ExitStatus.ACCEPTED, accepted_lwd=lwd,
                                                   decided_at=Coalesce(F('decided_at'), Now()))
    ex = EmployeeExit.objects.filter(pk=exit_id).values('company_id', 'employee_id', 'kind').get()
    company_id, employee_id = str(ex['company_id']), str(ex['employee_id'])
    Employee.objects.filter(pk=employee_id).update(exit_date=lwd, updated_at=Now())
    audit('accept_exit', 'employee_exit', actor_id=actor_id, row_id=exit_id, after={'lastWorkingDay': _iso(lwd), 'kind': ex['kind']})
    if not LifecycleCase.objects.filter(employee_id=employee_id, kind=LifecycleCase.Kind.OFFBOARDING, status=CaseStatus.ACTIVE).exists():
        start_case(company_id=company_id, employee_id=employee_id, kind='offboarding', template_id=template_id, anchor_date=lwd,
                   started_by=actor_id, exit_id=exit_id)
    own = user_id_for_employee(employee_id)
    if own and ex['kind'] != EmployeeExit.Kind.DEATH:
        notify(company_id=company_id, user_id=own, employee_id=employee_id, template='lifecycle.exit.accepted',
               title='Resignation accepted' if ex['kind'] == EmployeeExit.Kind.RESIGNATION else 'Your last working day',
               body=f'Your last working day is {bs_label(lwd)}. Your exit checklist is on Home.', payload={'exitId': exit_id},
               channels=('push',))


def apply_resignation_decision(exit_id: str, state: str, ctx) -> None:
    ex = (EmployeeExit.objects.select_for_update().filter(pk=exit_id)
          .values('status', 'proposed_lwd', 'accepted_lwd', 'employee_id', 'company_id').first())
    if not ex:
        raise NotFoundError('resignation not found')
    if ex['status'] != ExitStatus.PENDING:
        raise ConflictError(f"resignation already {ex['status']}")
    if state == 'approved':
        accept_exit(str(exit_id), ex['accepted_lwd'] or ex['proposed_lwd'], ctx.actor_id)
        return
    EmployeeExit.objects.filter(pk=exit_id).update(status=ExitStatus.REJECTED, decided_at=Now())
    own = user_id_for_employee(str(ex['employee_id']))
    if own:
        notify(company_id=str(ex['company_id']), user_id=own, employee_id=str(ex['employee_id']), template='lifecycle.exit.rejected',
               title='Resignation not accepted', body=f"{ctx.reason or 'Please talk to your manager or HR.'}", payload={'exitId': str(exit_id)})


def clearance_blockers(exit_id: str, employee_id: str) -> dict:
    """Open clearance tasks and assets still held, which stop an exit from being completed."""
    t = LifecycleTask.objects.filter(case__exit_id=exit_id, case__status=CaseStatus.ACTIVE, status=TaskStatus.OPEN).count()
    a = AssetAssignment.objects.filter(employee_id=employee_id, returned_on__isnull=True).count()
    return {'openTasks': t, 'assetsHeld': a}


def complete_exit(exit_id: str, actor_id: str | None, *, skip_open_tasks: bool = False) -> None:
    """Finish an exit: the employee becomes exited (sign-in already stops after the last working day)."""
    ex = (EmployeeExit.objects.select_for_update().filter(pk=exit_id)
          .values('status', 'employee_id', 'company_id', 'accepted_lwd', 'kind', 'reason').first())
    if not ex:
        raise NotFoundError('exit not found')
    if ex['status'] != ExitStatus.ACCEPTED:
        raise ConflictError(f"exit is {ex['status']}")
    if ex['accepted_lwd'] > npt_today():
        raise ConflictError(f"the last working day is {bs_label(ex['accepted_lwd'])}; complete the exit after it")
    employee_id = str(ex['employee_id'])
    b = clearance_blockers(exit_id, employee_id)
    if b['assetsHeld']:
        n = b['assetsHeld']
        raise ConflictError(f"{n} company {'asset is' if n == 1 else 'assets are'} still with the employee; record the return (or mark lost) first")
    if b['openTasks'] and not skip_open_tasks:
        n = b['openTasks']
        raise ConflictError(f"{n} clearance {'task is' if n == 1 else 'tasks are'} still open")
    if b['openTasks']:
        (LifecycleTask.objects.filter(case__exit_id=exit_id, status=TaskStatus.OPEN)
         .update(status=TaskStatus.SKIPPED, done_by_id=actor_id, done_at=Now(),
                 note=Coalesce(F('note'), Value('Skipped when the exit was completed'))))
    LifecycleCase.objects.filter(exit_id=exit_id, status=CaseStatus.ACTIVE).update(status=CaseStatus.COMPLETED, completed_at=Now())
    EmployeeExit.objects.filter(pk=exit_id).update(status=ExitStatus.COMPLETED, completed_at=Now())
    Employee.objects.filter(pk=employee_id).update(status=Employee.Status.EXITED, exit_date=ex['accepted_lwd'], updated_at=Now())
    audit('complete_exit', 'employee_exit', actor_id=actor_id, row_id=str(exit_id), after={'skippedTasks': b['openTasks']})
    log_career_event(company_id=str(ex['company_id']), employee_id=employee_id, kind='separation', effective_date=ex['accepted_lwd'],
                     details={'separationType': ex['kind']}, note=ex['reason'], actor_id=actor_id)


BS_MONTHS = ['Baishakh', 'Jestha', 'Ashadh', 'Shrawan', 'Bhadra', 'Ashwin', 'Kartik', 'Mangsir', 'Poush', 'Magh', 'Falgun', 'Chaitra']


def settlement_status(company_id: str, employee_id: str, lwd) -> dict:
    """What HR still needs to do in payroll for a leaver (final settlement run, loans, leave encashment)."""
    period = (PayrollPeriod.objects.for_company(company_id).filter(start_ad__lte=lwd, end_ad__gte=lwd)
              .values('id', 'bs_year', 'bs_month').first()) if lwd else None
    run = (PayrollRun.objects.for_company(company_id)
           .filter(run_type=PayrollRun.RunType.FINAL_SETTLEMENT, params__employeeIds__has_key=str(employee_id))
           .order_by('-created_at').values('id', 'status', 'locked_at').first())
    paid = (LoanInstallment.objects.filter(loan=OuterRef('pk')).order_by().values('loan')
            .annotate(s=Sum('amount')).values('s'))
    money = DecimalField(max_digits=14, decimal_places=2)
    loans = (Loan.objects.filter(employee_id=employee_id, status=Loan.Status.ACTIVE)
             .annotate(left=F('principal') - Coalesce(Subquery(paid, output_field=money), Value(Decimal(0)), output_field=money))
             .aggregate(n=Count('id'), outstanding=Sum('left')))
    leave = (LeaveLedger.objects.filter(employee_id=employee_id, leave_type__encashable=True).aggregate(days=Sum('delta_days'))['days']
             or 0)
    steps: list[str] = []
    if not run:
        steps.append(f"Create a final settlement run for {BS_MONTHS[period['bs_month'] - 1]} {period['bs_year']} (Payroll, New run, Final settlement)."
                     if period else 'Create the payroll period that contains the last working day, then a final settlement run for this employee.')
    elif not run['locked_at']:
        steps.append('Finish and lock the final settlement run.')
    if loans['n']:
        n = loans['n']
        steps.append(f"Recover {n} active {'loan or advance' if n == 1 else 'loans or advances'} in the settlement.")
    days = float(leave)
    if days > 0:
        steps.append(f'Encashable leave of {days:.1f} days is paid out in the settlement.')
    return {
        'period': {'id': str(period['id']), 'bsYear': period['bs_year'], 'bsMonth': period['bs_month']} if period else None,
        'run': {'id': str(run['id']), 'status': run['status'], 'locked': bool(run['locked_at'])} if run else None,
        'activeLoans': loans['n'], 'loanOutstanding': str(loans['outstanding']) if loans['outstanding'] is not None else '0',
        'encashableLeaveDays': int(days) if days.is_integer() else days,
        'steps': steps,
    }


def auto_start_onboarding(company_id: str, employee_id: str, join_date, actor_id: str | None) -> str | None:
    """
    Called when HR adds an employee: opens the default onboarding checklist when the company has switched that on.
    Runs in the caller's transaction behind a savepoint, so a checklist problem never stops the employee being created.
    """
    from hajiri.features.service import feature_on
    policy = lifecycle_policy(company_id)
    if not policy['autoStartOnboarding'] or not feature_on(company_id, 'lifecycle'):
        return None
    try:
        with transaction.atomic():
            return start_case(company_id=company_id, employee_id=str(employee_id), kind='onboarding', anchor_date=join_date,
                              started_by=actor_id)
    except Exception:  # noqa: BLE001 - as the TS savepoint: any failure leaves the employee created without a checklist
        return None


def notify_hr(company_id: str, **kw) -> int:
    """One notification per person with HR in the company (members of the group with access included)."""
    ids = user_ids_with_role(company_id, 'hr')
    for uid in ids:
        notify(company_id=company_id, user_id=uid, **kw)
    return len(ids)


# ---------------------------------------------------------------- daily job
def lifecycle_daily(now: datetime | None = None) -> dict:
    """Runs for every company (a background job has no active company); each step uses the row's own company."""
    today = npt_today(now)
    out = {'overdueReminders': 0, 'probationReminders': 0, 'exitsCompleted': 0, 'exitsFlagged': 0}

    # 1. Overdue checklist tasks: one reminder per task to whoever should act.
    overdue = (LifecycleTask.objects.filter(status=TaskStatus.OPEN, case__status=CaseStatus.ACTIVE, due_date__lt=today)
               .annotate(name_en=F('employee__name_en')).order_by('due_date').values(*TASK_COLS, 'name_en')[:2000])
    for t in overdue:
        t = task_row(t)
        if not remind_once(t['company_id'], 'task_overdue', t['id'], 'overdue'):
            continue
        for user_id in responsible_users(t):
            notify(company_id=t['company_id'], user_id=user_id, template='lifecycle.task.overdue', title='Checklist task overdue',
                   body=f'"{t["title"]}" for {t["name_en"]} was due on {bs_label(t["due_date"])}.', payload={'caseId': t['case_id'], 'taskId': t['id']})
        out['overdueReminders'] += 1

    # 2. Probation ending: remind the manager (HR when there is none) at each configured threshold.
    pending_review = ProbationReview.objects.filter(employee=OuterRef('pk'), status=ProbationReview.Status.PENDING)
    for cid in Company.objects.values_list('id', flat=True):
        cid = str(cid)
        policy = lifecycle_policy(cid)
        max_days = max([0, *policy['probationReminderDays']])
        if not max_days:
            continue
        due = (Employee.objects.for_company(cid)
               .filter(status=Employee.Status.ACTIVE, probation_end__isnull=False, confirmed_on__isnull=True,
                       probation_end__gte=today, probation_end__lte=today + timedelta(days=max_days))
               .filter(Q(exit_date__isnull=True) | Q(exit_date__gte=today)).exclude(Exists(pending_review))
               .values('id', 'name_en', 'probation_end'))
        for e in due:
            days_left = (e['probation_end'] - today).days
            threshold = next((d for d in sorted(policy['probationReminderDays']) if days_left <= d), None)
            if threshold is None:
                continue
            if not remind_once(cid, 'probation', str(e['id']), f"{e['probation_end'].isoformat()}:{threshold}"):
                continue
            mgr = manager_user_of(str(e['id']))
            targets = [mgr] if mgr else user_ids_with_role(cid, 'hr')
            n = days_left
            for user_id in targets:
                notify(company_id=cid, user_id=user_id, template='lifecycle.probation.ending',
                       title=f"Probation ends in {n} {'day' if n == 1 else 'days'}",
                       body=f"{e['name_en']}'s probation ends on {bs_label(e['probation_end'])}. Submit a probation review to confirm, extend or end it.",
                       payload={'employeeId': str(e['id'])}, channels=('push',))
            out['probationReminders'] += 1

    # 3. Exits past their last working day: complete when clearance is done, otherwise tell HR once.
    exits = (EmployeeExit.objects.filter(status=ExitStatus.ACCEPTED, accepted_lwd__lt=today)
             .annotate(name_en=F('employee__name_en')).values('id', 'company_id', 'employee_id', 'name_en'))
    for x in exits:
        xid, cid, eid = str(x['id']), str(x['company_id']), str(x['employee_id'])
        b = clearance_blockers(xid, eid)
        if not b['openTasks'] and not b['assetsHeld']:
            try:
                with transaction.atomic():
                    complete_exit(xid, None)
                out['exitsCompleted'] += 1
            except Exception:  # noqa: BLE001 - as the TS job: a failure is rolled back and retried tomorrow
                pass
            continue
        if not remind_once(cid, 'exit_pending', xid, 'clearance'):
            continue
        t, a = b['openTasks'], b['assetsHeld']
        notify_hr(cid, template='lifecycle.exit.clearance_pending', title=f"Exit clearance pending for {x['name_en']}",
                  body=f"The last working day has passed. {t} clearance {'task is' if t == 1 else 'tasks are'} open and "
                       f"{a} {'asset is' if a == 1 else 'assets are'} not returned.", payload={'exitId': xid})
        out['exitsFlagged'] += 1
    return out


def diff_days(a, b) -> int:
    """Calendar days from b to a."""
    return (_d(a) - _d(b)).days


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)
