"""
Employee lifecycle routes (port of apps/api/src/modules/lifecycle/routes.ts): onboarding and exit checklists,
probation, resignations and exits, the exit interview and the asset register. Each function is one TS route: it takes
the signed-in AuthUser and a body in the TS camelCase shape (validated with serializers.py) and raises
hajiri.core.errors with the TS messages. Everything is scoped to the ACTIVE company; ids of another company are not
found. (The module keeps its TS name; the pages call these functions, and so do the DRF views of the JSON API under
/api/v1/lifecycle in api_views.py.)
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.db.models import (BooleanField, Count, Exists, ExpressionWrapper, F, FilteredRelation, Func, IntegerField, Max, Min,
                              OuterRef, Q, Subquery, Value)
from django.db.models.functions import Coalesce, Greatest, JSONObject, Least, Now

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role, is_manager_of, manages
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import npt_today
from hajiri.core.crypto import field_cipher
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.scoping import is_uuid
from hajiri.core.settings_store import put_setting
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (AppUser, Asset, AssetAssignment, AssetType, Branch, Employee, EmployeeAssignment, EmployeeConfirmation,
                              EmployeeExit,
                              ExitInterview, ExitInterviewQuestion, LifecycleCase, LifecycleTask, LifecycleTaskComment,
                              LifecycleTemplate, LifecycleTemplateTask, OrgUnit, ProbationReview)
from hajiri.notifications.service import notify
from hajiri.people.accounts import user_id_for_employee
from hajiri.people.serializers import one_of

from . import service as svc
from .filters import AssetFilter
from .serializers import (CASE_KINDS, CASE_STATUSES, AddTaskIn, AssetIn, AssetQuery, AssetTypeIn, AssetUpdateIn, AssignAssetIn,
                          CancelExitIn, CommentIn, CompleteExitIn, ConfirmIn, InterviewIn, PolicyIn, ProbationQuery, QuestionsIn,
                          RecordExitIn, ResignIn, ReturnAssetIn, ReviewIn, StartCaseIn, TemplateIn, UpdateExitIn, UpdateTaskIn)
from .service import (EXIT_KINDS, TASK_COLS, apply_confirmation, can_complete_task, can_view_case, clearance_blockers,  # noqa: F401
                      complete_case_if_done, complete_exit, diff_days, lifecycle_policy, record_exit, responsible_users,
                      settlement_status, start_case, task_row)

CaseStatus = LifecycleCase.Status
TaskStatus = LifecycleTask.Status
ExitStatus = EmployeeExit.Status
ASSIGNEES = tuple(LifecycleTask.AssigneeType.values)
EXIT_KIND_LABEL = {'resignation': 'Resignation', 'termination': 'Termination', 'retirement': 'Retirement', 'contract_end': 'Contract ended',
                   'death': 'Death', 'other': 'Other'}
ASSIGNEE_LABEL = {'hr': 'HR', 'manager': 'Manager', 'employee': 'Employee', 'user': 'Named person'}
RESIGN_REASONS = ['Better pay', 'Career growth', 'Work-life balance', 'Manager or team', 'Relocation', 'Further studies', 'Health or family', 'Other']
CONDITIONS = tuple(Asset.Condition.values)
CASE_COLS = ('id', 'company_id', 'employee_id', 'kind', 'template_id', 'anchor_date', 'status', 'exit_id', 'started_by', 'created_at',
             'completed_at')


def _s(r: dict | None, *keys: str) -> dict | None:
    if r is None:
        return None
    for k in keys:
        if r.get(k) is not None:
            r[k] = str(r[k])
    return r


def _flag(q: Q) -> ExpressionWrapper:
    return ExpressionWrapper(q, output_field=BooleanField())


def count_of(qs) -> Subquery:
    """count(*) of a correlated queryset as a subquery (COUNT without GROUP BY, so it is one row)."""
    return Subquery(qs.order_by().annotate(n=Func(F('pk'), function='COUNT', output_field=IntegerField())).values('n'),
                    output_field=IntegerField())


def _is_hr(user: AuthUser) -> bool:
    return has_role(user, 'hr')


def assert_manages(user: AuthUser, employee_id: str) -> None:
    """The TS lifecycle routes use the workflow helper ('not allowed to act for this employee'). An employee of another
    company is not found, before any role check."""
    from hajiri.workflow.services import assert_manages as wf_assert
    employee_in(company_id_of(user), employee_id)
    wf_assert(user, employee_id)


def _require(user: AuthUser, *roles: str) -> None:
    if not has_role(user, *roles):
        raise ForbiddenError(f"requires role {' or '.join(roles)}")


def _managed(user: AuthUser, on_date) -> Q | ExpressionWrapper:
    """HR sees every row; a manager the employees they manage on that date."""
    if _is_hr(user):
        return Q()
    return manages(user.employee_id, F('employee_id'), on_date) if user.employee_id else Q(pk__in=[])


def employee_in(company_id: str, id_: str) -> dict:
    r = (Employee.objects.for_company(company_id).filter(pk=id_)
         .values('id', 'name_en', 'join_date', 'probation_end', 'confirmed_on', 'status', 'exit_date').first()) if is_uuid(id_) else None
    if not r:
        raise NotFoundError('employee not found')
    return _s(r, 'id')


def users_in_company(company_id: str, ids) -> None:
    """Named people on tasks must be able to act in the company (home accounts, members of the group with access)."""
    lst = list(dict.fromkeys(str(x) for x in ids if x))
    if not lst:
        return
    n = AppUser.objects.for_company(company_id).filter(pk__in=lst, status=AppUser.Status.ACTIVE).count()
    if n != len(lst):
        raise BadRequestError('the chosen person is not an active user in your company')


def _unique(e: Exception) -> bool:
    return getattr(getattr(e, '__cause__', None), 'sqlstate', None) == '23505'


def ensure_defaults(user: AuthUser) -> None:
    """Defaults are seeded on first use, so companies that existed before this module get them too."""
    if user and user.company_id:
        svc.ensure_lifecycle_defaults(user.company_id)


# ------------------------------------------------------------ settings

def get_settings(user: AuthUser) -> dict:
    return lifecycle_policy(company_id_of(user))


def save_settings(user: AuthUser, body: dict) -> dict:
    _require(user, 'hr')
    company_id = company_id_of(user)
    policy = validate(PolicyIn, body)
    before = lifecycle_policy(company_id)
    with transaction.atomic():
        put_setting(company_id, svc.POLICY_KEY, policy, updated_by=user.id)
        audit('update', 'company_setting', actor_id=user.id, row_id=svc.POLICY_KEY, before=before, after=policy)
    return policy


# ------------------------------------------------------------ templates

TEMPLATE_COLS = ('id', 'company_id', 'kind', 'name', 'description', 'branch_id', 'org_unit_id', 'is_default', 'active', 'created_at',
                 'updated_at')
TEMPLATE_TASK_COLS = ('id', 'template_id', 'sort', 'title', 'description', 'department', 'assignee_type', 'assignee_user_id',
                      'due_offset_days', 'link')


def templates(user: AuthUser, kind: str | None = None) -> list[dict]:
    _require(user, 'hr', 'manager')
    qs = LifecycleTemplate.objects.for_company(company_id_of(user))
    if kind:
        qs = qs.filter(kind=one_of(CASE_KINDS, kind))
    out = [_s(r, 'id', 'company_id', 'branch_id', 'org_unit_id') for r in
           qs.annotate(branch_name=F('branch__name_en'), org_unit_name=F('org_unit__name_en'))
           .order_by('kind', '-is_default', 'name').values(*TEMPLATE_COLS, 'branch_name', 'org_unit_name')]
    tasks: dict[str, list] = {}
    for t in (LifecycleTemplateTask.objects.filter(template_id__in=[r['id'] for r in out]).order_by('sort')
              .values(*TEMPLATE_TASK_COLS)):
        tasks.setdefault(str(t['template_id']), []).append(_s(t, 'id', 'template_id', 'assignee_user_id'))
    for r in out:
        r['tasks'] = tasks.get(r['id'], [])
    return out


def get_template(user: AuthUser, id_: str) -> dict:
    t = next((x for x in templates(user) if x['id'] == str(id_)), None)
    if not t:
        raise NotFoundError('template not found')
    return t


def save_template(user: AuthUser, id_: str | None, body: dict) -> str:
    _require(user, 'hr')
    company_id = company_id_of(user)
    if id_ is not None:
        id_ = parse_uuid(str(id_))
    b = plain(validate(TemplateIn, body))
    users_in_company(company_id, [t.get('assigneeUserId') for t in b['tasks']])
    for model, ref, label in ((Branch, b.get('branchId'), 'branch'), (OrgUnit, b.get('orgUnitId'), 'org unit')):
        if ref and not model.objects.for_company(company_id).filter(pk=ref).exists():
            raise BadRequestError(f'{label} not found')
    fields = {'name': b['name'], 'description': b.get('description'), 'branch_id': b.get('branchId'), 'org_unit_id': b.get('orgUnitId'),
              'is_default': b['isDefault'], 'active': b['active']}
    mine = LifecycleTemplate.objects.for_company(company_id)
    with transaction.atomic():
        tid = id_
        if b['isDefault']:
            mine.filter(kind=b['kind']).update(is_default=False)
        if tid:
            if not mine.filter(pk=tid, kind=b['kind']).update(**fields, updated_at=Now()):
                raise NotFoundError('template not found')
            LifecycleTemplateTask.objects.filter(template_id=tid).delete()
        else:
            try:
                with transaction.atomic():
                    tid = str(LifecycleTemplate.objects.create(company_id=company_id, kind=b['kind'], **fields).pk)
            except IntegrityError as e:
                if _unique(e):
                    raise ConflictError('a checklist with this name already exists') from e
                raise
        LifecycleTemplateTask.objects.bulk_create([
            LifecycleTemplateTask(template_id=tid, sort=i, title=t['title'], description=t.get('description'), department=t.get('department'),
                                  assignee_type=t['assigneeType'], assignee_user_id=t.get('assigneeUserId') if t['assigneeType'] == 'user' else None,
                                  due_offset_days=t['dueOffsetDays'], link=t.get('link'))
            for i, t in enumerate(b['tasks'])])
        audit('update' if id_ else 'create', 'lifecycle_template', actor_id=user.id, row_id=tid,
              after={'name': b['name'], 'kind': b['kind'], 'tasks': len(b['tasks'])})
    return str(tid)


# ------------------------------------------------------------ cases and tasks

def cases(user: AuthUser, kind: str | None = None, status: str | None = None) -> list[dict]:
    _require(user, 'hr', 'manager')
    qs = LifecycleCase.objects.for_company(company_id_of(user))
    if kind:
        qs = qs.filter(kind=one_of(CASE_KINDS, kind))
    if status:
        qs = qs.filter(status=one_of(CASE_STATUSES, status))
    today = npt_today()
    open_ = Q(lifecycle_tasks__status=TaskStatus.OPEN)
    qs = (qs.filter(_managed(user, Greatest(Value(today), F('anchor_date'))))
          .annotate(emp_code=F('employee__emp_code'), name_en=F('employee__name_en'), designation=F('employee__designation'),
                    join_date=F('employee__join_date'), template_name=F('template__name'),
                    total=Count('lifecycle_tasks'), done=Count('lifecycle_tasks', filter=~open_),
                    overdue=Count('lifecycle_tasks', filter=open_ & Q(lifecycle_tasks__due_date__lt=today)),
                    next_due=Min('lifecycle_tasks__due_date', filter=open_))
          .order_by(_flag(Q(status=CaseStatus.ACTIVE)).desc(), '-anchor_date')
          .values('id', 'kind', 'status', 'anchor_date', 'created_at', 'completed_at', 'exit_id', 'employee_id', 'emp_code', 'name_en',
                  'designation', 'join_date', 'template_name', 'total', 'done', 'overdue', 'next_due'))
    return [_s(r, 'id', 'exit_id', 'employee_id') for r in qs[:500]]


def start_onboarding(user: AuthUser, body: dict) -> str:
    _require(user, 'hr')
    company_id = company_id_of(user)
    b = plain(validate(StartCaseIn, body))
    e = employee_in(company_id, b['employeeId'])
    if e['status'] == Employee.Status.EXITED:
        raise BadRequestError('this employee has left')
    with transaction.atomic():
        return start_case(company_id=company_id, employee_id=b['employeeId'], kind='onboarding', template_id=b.get('templateId'),
                          anchor_date=b.get('anchorDate') or e['join_date'] or npt_today(), started_by=user.id)


def load_case(user: AuthUser, id_: str) -> dict:
    c = _s(LifecycleCase.objects.for_company(user.company_id).filter(pk=parse_uuid(str(id_))).values(*CASE_COLS).first(),
           'id', 'company_id', 'employee_id', 'exit_id', 'template_id', 'started_by')
    if not c or not can_view_case(user, c):
        raise NotFoundError('checklist not found')
    return c


def case_detail(user: AuthUser, id_: str) -> dict:
    c = load_case(user, id_)
    emp = _s(Employee.objects.filter(pk=c['employee_id']).values('id', 'emp_code', 'name_en', 'designation', 'join_date', 'exit_date').first(),
             'id')
    tasks = (LifecycleTask.objects.filter(case_id=c['id'])
             .annotate(assignee_name=F('assignee_user__name'), done_by_name=F('done_by__name'),
                       comments=count_of(LifecycleTaskComment.objects.filter(task=OuterRef('pk'))))
             .order_by('sort', 'due_date').values(*TASK_COLS, 'assignee_name', 'done_by_name', 'comments'))
    out = []
    allowed: dict[tuple, bool] = {}   # the tasks share the case's employee: one check per kind of owner, not per task
    for t in tasks:
        t = task_row(t)
        key = (t['assignee_type'], t['assignee_user_id'])
        if key not in allowed:
            allowed[key] = can_complete_task(user, t)
        t['can_complete'] = allowed[key]
        out.append(t)
    return {'case': c, 'employee': emp, 'tasks': out}


def cancel_case(user: AuthUser, id_: str) -> None:
    _require(user, 'hr')
    c = load_case(user, id_)
    if c['status'] != CaseStatus.ACTIVE:
        raise ConflictError(f"checklist is {c['status']}")
    if c['exit_id']:
        raise BadRequestError('cancel the exit instead; its clearance checklist closes with it')
    with transaction.atomic():
        LifecycleCase.objects.filter(pk=c['id']).update(status=CaseStatus.CANCELLED, completed_at=Now())
        audit('cancel', 'lifecycle_case', actor_id=user.id, row_id=c['id'])


def add_task(user: AuthUser, case_id: str, body: dict) -> dict:
    _require(user, 'hr')
    c = load_case(user, case_id)
    if c['status'] != CaseStatus.ACTIVE:
        raise ConflictError(f"checklist is {c['status']}")
    b = plain(validate(AddTaskIn, body))
    if b['assigneeType'] == 'user' and not b.get('assigneeUserId'):
        raise BadRequestError('choose the person for this task')
    users_in_company(c['company_id'], [b.get('assigneeUserId')])
    with transaction.atomic():
        LifecycleCase.objects.select_for_update(no_key=True).filter(pk=c['id']).values_list('id', flat=True).first()
        last = LifecycleTask.objects.filter(case_id=c['id']).aggregate(m=Max('sort'))['m'] or 0
        created = LifecycleTask.objects.create(
            case_id=c['id'], company_id=c['company_id'], employee_id=c['employee_id'], sort=last + 1, title=b['title'],
            description=b.get('description'), department=b.get('department'), assignee_type=b['assigneeType'],
            assignee_user_id=b.get('assigneeUserId') if b['assigneeType'] == 'user' else None, due_date=b.get('dueDate'),
            link=b.get('link'))
        t = task_row(LifecycleTask.objects.filter(pk=created.pk).values(*TASK_COLS).get())
        for uid in responsible_users(t):
            if uid != user.id:
                notify(company_id=c['company_id'], user_id=uid, template='lifecycle.task.assigned', title='New checklist task',
                       body=b['title'], payload={'caseId': c['id']})
    return t


def load_task(user: AuthUser, id_: str) -> dict:
    t = task_row(LifecycleTask.objects.for_company(user.company_id).filter(pk=parse_uuid(str(id_))).values(*TASK_COLS).first())
    if not t:
        raise NotFoundError('task not found')
    load_case(user, t['case_id'])
    return t


def update_task(user: AuthUser, id_: str, body: dict) -> dict:
    t = load_task(user, id_)
    b = plain(validate(UpdateTaskIn, body))
    case_status = LifecycleCase.objects.filter(pk=t['case_id']).values_list('status', flat=True).get()
    if case_status == CaseStatus.CANCELLED:
        raise ConflictError('this checklist was cancelled')
    edits_plan = 'dueDate' in b or 'assigneeType' in b or 'assigneeUserId' in b
    if edits_plan and not _is_hr(user):
        raise ForbiddenError('only HR can change due dates and owners')
    if ('status' in b or 'note' in b) and not can_complete_task(user, t):
        raise ForbiddenError('this task is for someone else')
    if b.get('status') == TaskStatus.SKIPPED and not _is_hr(user):
        raise ForbiddenError('only HR can skip a task')
    if b.get('assigneeType') == 'user' and not b.get('assigneeUserId'):
        raise BadRequestError('choose the person for this task')
    users_in_company(t['company_id'], [b.get('assigneeUserId')])
    status = b.get('status')
    sets: dict = {}
    if status:
        sets['status'] = status
    if 'note' in b:
        sets['note'] = b['note']
    if status in (TaskStatus.DONE, TaskStatus.SKIPPED):
        sets.update(done_by_id=user.id, done_at=Now())
    elif status == TaskStatus.OPEN:
        sets.update(done_by_id=None, done_at=None)
    if 'dueDate' in b:
        sets['due_date'] = b['dueDate']
    if b.get('assigneeType'):
        sets['assignee_type'] = b['assigneeType']
        sets['assignee_user_id'] = b.get('assigneeUserId') if b['assigneeType'] == 'user' else None
    with transaction.atomic():
        if sets:
            LifecycleTask.objects.filter(pk=t['id']).update(**sets)
        u = task_row(LifecycleTask.objects.filter(pk=t['id']).values(*TASK_COLS).get())
        # Clearance checklists stay open until the exit is completed; onboarding closes itself when everything is ticked.
        completed = False
        if status in (TaskStatus.DONE, TaskStatus.SKIPPED):
            exit_id = LifecycleCase.objects.filter(pk=t['case_id']).values_list('exit_id', flat=True).first()
            if not exit_id:
                completed = complete_case_if_done(t['case_id'])
        if status == TaskStatus.OPEN:
            (LifecycleCase.objects.filter(pk=t['case_id'], status=CaseStatus.COMPLETED, exit_id__isnull=True)
             .update(status=CaseStatus.ACTIVE, completed_at=None))
        audit('update', 'lifecycle_task', actor_id=user.id, row_id=t['id'], before={'status': t['status']}, after=b)
    return {'task': u, 'caseCompleted': completed}


def comments(user: AuthUser, task_id: str) -> list[dict]:
    t = load_task(user, task_id)
    return list(LifecycleTaskComment.objects.filter(task_id=t['id']).annotate(author_name=F('author__name')).order_by('created_at')
                .values('id', 'body', 'created_at', 'author_id', 'author_name'))


def add_comment(user: AuthUser, task_id: str, body: dict) -> dict:
    t = load_task(user, task_id)
    b = validate(CommentIn, body)
    with transaction.atomic():
        c = LifecycleTaskComment.objects.create(task_id=t['id'], author_id=user.id, body=b['body'])
        ins = LifecycleTaskComment.objects.filter(pk=c.pk).values('id', 'body', 'created_at').get()
        for uid in responsible_users(t):
            if uid != user.id:
                notify(company_id=t['company_id'], user_id=uid, template='lifecycle.task.comment', title=f'Comment on "{t["title"]}"',
                       body=b['body'][:200], payload={'caseId': t['case_id'], 'taskId': t['id']})
    return ins


def my_tasks(user: AuthUser) -> list[dict]:
    """Open checklist tasks for me: as the employee, as the named person, as the line manager (on the later of today
    and the case's anchor date), and, for HR, HR's tasks and manager tasks of people with no manager."""
    today = npt_today()
    manager_then = Subquery(
        EmployeeAssignment.objects.filter(employee_id=OuterRef('employee_id'),
                                          valid__contains=Greatest(Value(today), OuterRef('case__anchor_date')))
        .values('manager_id')[:1])
    me = user.employee_id
    A = LifecycleTask.AssigneeType
    mine = Q(assignee_type=A.USER, assignee_user_id=user.id)
    if me:
        mine |= Q(assignee_type=A.EMPLOYEE, employee_id=me) | Q(assignee_type=A.MANAGER, line_manager=me)
    if _is_hr(user):
        mine |= Q(assignee_type=A.HR) | Q(assignee_type=A.MANAGER, line_manager__isnull=True)
    qs = (LifecycleTask.objects.for_company(company_id_of(user)).filter(case__status=CaseStatus.ACTIVE, status=TaskStatus.OPEN)
          .annotate(line_manager=manager_then, kind=F('case__kind'), employee_name=F('employee__name_en'), emp_code=F('employee__emp_code'),
                    own=_flag(Q(employee_id=me)) if me else Value(False, output_field=BooleanField()))
          .filter(mine).order_by(F('due_date').asc(nulls_last=True), 'sort')
          .values(*TASK_COLS, 'kind', 'employee_name', 'emp_code', 'own'))
    return [task_row(r) for r in qs[:200]]


# ------------------------------------------------------------ one employee at a glance (employee page)

def employee_summary(user: AuthUser, id_: str) -> dict:
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    e = employee_in(company_id, id_)
    assert_manages(user, id_)
    open_ = Q(lifecycle_tasks__status=TaskStatus.OPEN)
    cs = [_s(r, 'id') for r in LifecycleCase.objects.for_company(company_id).filter(employee_id=id_)
          .annotate(total=Count('lifecycle_tasks'), done=Count('lifecycle_tasks', filter=~open_)).order_by('-created_at')
          .values('id', 'kind', 'status', 'anchor_date', 'created_at', 'total', 'done')]
    reviews = [_s(r, 'id') for r in ProbationReview.objects.for_company(company_id).filter(employee_id=id_)
               .annotate(submitted_by_name=F('submitted_by__name')).order_by('-created_at')
               .values('id', 'rating', 'recommendation', 'extend_to', 'last_working_day', 'status', 'created_at', 'probation_end',
                       'submitted_by_name')]
    confirmations = [_s(r, 'id', 'review_id') for r in EmployeeConfirmation.objects.for_company(company_id).filter(employee_id=id_)
                     .order_by('-created_at').values('id', 'confirmed_on', 'review_id', 'created_at')]
    exits = [_s(r, 'id') for r in EmployeeExit.objects.for_company(company_id).filter(employee_id=id_).order_by('-created_at')
             .values('id', 'kind', 'status', 'voluntary', 'reason_category', 'proposed_lwd', 'accepted_lwd', 'submitted_on')]
    assets = [_s(r, 'id', 'assignment_id') for r in AssetAssignment.objects.for_company(company_id).filter(employee_id=id_)
              .annotate(assignment_id=F('pk'), asset_ref=F('asset_id'), tag=F('asset__tag'), name=F('asset__name'), serial=F('asset__serial'),
                        type_name=F('asset__type__name'))
              .order_by(_flag(Q(returned_on__isnull=True)).desc(), '-assigned_on')
              .values('assignment_id', 'assigned_on', 'returned_on', 'condition_out', 'condition_in', 'asset_ref', 'tag', 'name',
                      'serial', 'type_name')]
    for a in assets:
        a['id'] = str(a.pop('asset_ref'))
    return {'employee': e, 'cases': cs,
            'probation': {'end': e['probation_end'], 'confirmedOn': e['confirmed_on'], 'reviews': reviews, 'confirmations': confirmations},
            'exits': exits, 'assets': assets}


# ------------------------------------------------------------ probation

def probation(user: AuthUser, within: int | str | None = 60) -> list[dict]:
    _require(user, 'hr', 'manager')
    within = validate(ProbationQuery, {} if within in (None, '') else {'within': within})['within']
    today = npt_today()
    last_review = (ProbationReview.objects.filter(employee=OuterRef('pk')).order_by('-created_at')
                   .annotate(j=JSONObject(id='id', status='status', recommendation='recommendation', created_at='created_at'))
                   .values('j')[:1])
    qs = (Employee.objects.for_company(company_id_of(user))
          .filter(status=Employee.Status.ACTIVE, probation_end__isnull=False, confirmed_on__isnull=True,
                  probation_end__lte=today + timedelta(days=within))
          .filter(Q(exit_date__isnull=True) | Q(exit_date__gte=today))
          .filter(Q() if _is_hr(user) else (manages(user.employee_id) if user.employee_id else Q(pk__in=[])))
          .annotate(a=FilteredRelation('employee_assignments', condition=Q(employee_assignments__valid__contains=today)))
          .annotate(manager_name=F('a__manager__name_en'), last_review=Subquery(last_review))
          .order_by('probation_end')
          .values('id', 'emp_code', 'name_en', 'designation', 'join_date', 'probation_end', 'manager_name', 'last_review'))
    out = []
    for r in qs:
        r = _s(r, 'id')
        r['days_left'] = (r['probation_end'] - today).days
        out.append(r)
    return out


def submit_review(user: AuthUser, body: dict) -> str:
    from hajiri.workflow.services import start_workflow
    _require(user, 'hr', 'manager')
    company_id = company_id_of(user)
    b = plain(validate(ReviewIn, body))
    e = employee_in(company_id, b['employeeId'])
    if b['employeeId'] == user.employee_id:
        raise ForbiddenError('you cannot review your own probation')
    assert_manages(user, b['employeeId'])
    if e['status'] != Employee.Status.ACTIVE:
        raise BadRequestError('this employee has left')
    if e['confirmed_on']:
        raise ConflictError(f"already confirmed on {bs_label(e['confirmed_on'])}")
    Rec = ProbationReview.Recommendation
    if b['recommendation'] == Rec.EXTEND:
        if not b.get('extendTo'):
            raise BadRequestError('choose the new probation end date')
        if date.fromisoformat(b['extendTo']) <= (e['probation_end'] or npt_today()):
            raise BadRequestError('the new end date must be after the current probation end')
    if b['recommendation'] == Rec.TERMINATE:
        if not b.get('lastWorkingDay'):
            raise BadRequestError('choose the last working day')
        if e['join_date'] and date.fromisoformat(b['lastWorkingDay']) < e['join_date']:
            raise BadRequestError('the last working day cannot be before the join date')
    try:
        with transaction.atomic():
            rid = str(ProbationReview.objects.create(
                company_id=company_id, employee_id=b['employeeId'], probation_end=e['probation_end'], rating=b['rating'],
                recommendation=b['recommendation'], extend_to=b.get('extendTo') if b['recommendation'] == Rec.EXTEND else None,
                last_working_day=b.get('lastWorkingDay') if b['recommendation'] == Rec.TERMINATE else None, comments=b['comments'],
                submitted_by_id=user.id).pk)
            rec = {'confirm': 'confirm', 'extend': 'extend probation'}.get(b['recommendation'], 'end employment')
            start_workflow(company_id=company_id, object_type='probation_review', object_id=rid, employee_id=b['employeeId'],
                           title='Probation review to approve', body=f"{e['name_en']}: {rec}")
            audit('create', 'probation_review', actor_id=user.id, row_id=rid, after={'rating': b['rating'], 'recommendation': b['recommendation']})
    except IntegrityError as ex:
        if _unique(ex):
            raise ConflictError('a probation review is already waiting for approval') from ex
        raise
    return rid


def confirm(user: AuthUser, body: dict) -> str:
    _require(user, 'hr')
    company_id = company_id_of(user)
    b = plain(validate(ConfirmIn, body))
    e = employee_in(company_id, b['employeeId'])
    if e['confirmed_on']:
        raise ConflictError(f"already confirmed on {bs_label(e['confirmed_on'])}")
    if e['join_date'] and date.fromisoformat(b['confirmedOn']) < e['join_date']:
        raise BadRequestError('confirmation cannot be before the join date')
    with transaction.atomic():
        return apply_confirmation(company_id=company_id, employee_id=b['employeeId'], confirmed_on=b['confirmedOn'], review_id=None,
                                  actor_id=user.id)


# ------------------------------------------------------------ exits

EXIT_STATUSES = (*ExitStatus.values, 'open')
OPEN_EXIT = (ExitStatus.PENDING, ExitStatus.ACCEPTED)
EXIT_FIELDS = ('id', 'employee_id', 'kind', 'voluntary', 'reason_category', 'reason', 'submitted_on', 'notice_days', 'proposed_lwd',
               'accepted_lwd', 'status', 'decided_at', 'completed_at', 'created_at', 'emp_code', 'name_en', 'designation', 'join_date',
               'case_id', 'tasks_total', 'tasks_done', 'assets_held', 'interview_done')


def exit_rows(qs):
    """Exits with the employee, the clearance checklist and its progress, assets still held and the interview flag
    (EXIT_COLS of the TS route)."""
    clearance = LifecycleTask.objects.filter(case__exit_id=OuterRef('pk')).exclude(case__status=CaseStatus.CANCELLED)
    return (qs.annotate(
        emp_code=F('employee__emp_code'), name_en=F('employee__name_en'), designation=F('employee__designation'),
        join_date=F('employee__join_date'),
        case_id=Subquery(LifecycleCase.objects.filter(exit_id=OuterRef('pk')).order_by('-created_at').values('id')[:1]),
        tasks_total=count_of(clearance), tasks_done=count_of(clearance.exclude(status=TaskStatus.OPEN)),
        assets_held=count_of(AssetAssignment.objects.filter(employee_id=OuterRef('employee_id'), returned_on__isnull=True)),
        interview_done=Exists(ExitInterview.objects.filter(exit=OuterRef('pk'))))
        .values(*EXIT_FIELDS))


def _exit(r):
    r = _s(r, 'id', 'employee_id', 'case_id')
    if r is not None:
        r['lwd'] = r['accepted_lwd'] or r['proposed_lwd']
    return r


def exits(user: AuthUser, status: str | None = None) -> list[dict]:
    _require(user, 'hr', 'manager')
    qs = EmployeeExit.objects.for_company(company_id_of(user))
    if status:
        one_of(EXIT_STATUSES, status)
        qs = qs.filter(status__in=OPEN_EXIT) if status == 'open' else qs.filter(status=status)
    lwd = Coalesce(F('accepted_lwd'), F('proposed_lwd'))
    qs = (qs.filter(_managed(user, Least(Value(npt_today()), lwd)))
          .order_by(_flag(Q(status__in=OPEN_EXIT)).desc(), lwd.desc()))
    return [_exit(r) for r in exit_rows(qs)[:500]]


def my_exits(user: AuthUser) -> dict:
    policy = lifecycle_policy(company_id_of(user))
    if not user.employee_id:
        return {'exits': [], 'policy': policy}
    qs = EmployeeExit.objects.for_company(company_id_of(user)).filter(employee_id=user.employee_id).order_by('-created_at')
    return {'exits': [_exit(r) for r in exit_rows(qs)], 'policy': policy}


def resign(user: AuthUser, body: dict) -> dict:
    from hajiri.workflow.services import start_workflow
    company_id = company_id_of(user)
    employee_id = user.employee_id
    if not employee_id:
        raise BadRequestError('your account is not linked to an employee record')
    b = plain(validate(ResignIn, body))
    e = employee_in(company_id, employee_id)
    if e['status'] != Employee.Status.ACTIVE:
        raise BadRequestError('your employment has already ended')
    today = npt_today()
    lwd = date.fromisoformat(b['proposedLastWorkingDay'])
    if lwd < today:
        raise BadRequestError('the last working day cannot be in the past')
    if diff_days(lwd, today) > 365:
        raise BadRequestError('the last working day must be within a year')
    policy = lifecycle_policy(company_id)
    try:
        with transaction.atomic():
            xid = str(EmployeeExit.objects.create(
                company_id=company_id, employee_id=employee_id, kind=EmployeeExit.Kind.RESIGNATION, voluntary=True,
                reason_category=b.get('reasonCategory'), reason=b.get('reason'), submitted_on=today, notice_days=policy['noticeDays'],
                proposed_lwd=lwd, initiated_by_id=user.id).pk)
            start_workflow(company_id=company_id, object_type='resignation', object_id=xid, employee_id=employee_id, title='Resignation submitted',
                           body=f"{e['name_en']} has resigned. Proposed last working day {bs_label(lwd)}.")
            audit('resign', 'employee_exit', actor_id=user.id, row_id=xid, after={'proposedLwd': b['proposedLastWorkingDay']})
    except IntegrityError as ex:
        if _unique(ex):
            raise ConflictError('you already have a resignation in progress') from ex
        raise
    shortfall = max(0, policy['noticeDays'] - diff_days(lwd, today))
    return {'id': xid, 'noticeDays': policy['noticeDays'], 'shortfallDays': shortfall}


def create_exit(user: AuthUser, body: dict) -> str:
    _require(user, 'hr')
    company_id = company_id_of(user)
    b = plain(validate(RecordExitIn, body))
    e = employee_in(company_id, b['employeeId'])
    if e['status'] != Employee.Status.ACTIVE:
        raise BadRequestError('this employee has already left')
    lwd = date.fromisoformat(b['lastWorkingDay'])
    if e['join_date'] and lwd < e['join_date']:
        raise BadRequestError('the last working day cannot be before the join date')
    if b['employeeId'] == user.employee_id:
        raise ForbiddenError('another HR user must record your own exit')
    voluntary = b['voluntary'] if 'voluntary' in b else b['kind'] in ('resignation', 'retirement')
    with transaction.atomic():
        return record_exit(company_id=company_id, employee_id=b['employeeId'], kind=b['kind'], voluntary=voluntary,
                           reason_category=b.get('reasonCategory'), reason=b.get('reason'), last_working_day=lwd, actor_id=user.id,
                           template_id=b.get('templateId'))


def load_exit(user: AuthUser, id_: str) -> dict:
    x = (EmployeeExit.objects.for_company(user.company_id).filter(pk=parse_uuid(str(id_)))
         .values('id', 'company_id', 'employee_id', 'status', 'kind', 'proposed_lwd', 'accepted_lwd').first())
    if not x:
        raise NotFoundError('exit not found')
    x = _s(x, 'id', 'company_id', 'employee_id')
    lwd = x['accepted_lwd'] or x['proposed_lwd']
    today = npt_today()
    as_of = lwd if lwd < today else today
    allowed = (has_role(user, 'hr') or user.employee_id == x['employee_id']
               or (bool(user.employee_id) and has_role(user, 'manager') and is_manager_of(user.employee_id, x['employee_id'], as_of)))
    if not allowed:
        raise NotFoundError('exit not found')
    return x


def exit_detail(user: AuthUser, id_: str) -> dict:
    x = load_exit(user, id_)
    row = _exit(exit_rows(EmployeeExit.objects.filter(pk=x['id'])).get())
    return {'exit': row, 'blockers': clearance_blockers(x['id'], x['employee_id']),
            'settlement': settlement_status(x['company_id'], x['employee_id'], x['accepted_lwd'] or x['proposed_lwd']) if _is_hr(user) else None}


def update_exit(user: AuthUser, id_: str, body: dict) -> None:
    _require(user, 'hr')
    x = load_exit(user, id_)
    b = plain(validate(UpdateExitIn, body))
    if x['status'] not in OPEN_EXIT:
        raise ConflictError(f"exit is {x['status']}")
    sets = {col: b[k] for k, col in (('reasonCategory', 'reason_category'), ('reason', 'reason')) if k in b}
    if b.get('voluntary') is not None:
        sets['voluntary'] = b['voluntary']
    if b.get('lastWorkingDay'):
        sets['accepted_lwd'] = b['lastWorkingDay']
    with transaction.atomic():
        if sets:
            EmployeeExit.objects.filter(pk=x['id']).update(**sets)
        if b.get('lastWorkingDay') and x['status'] == ExitStatus.ACCEPTED:
            lwd = date.fromisoformat(b['lastWorkingDay'])
            Employee.objects.filter(pk=x['employee_id']).update(exit_date=lwd, updated_at=Now())
            # Clearance due dates follow the new last working day.
            for case_id, anchor in (LifecycleCase.objects.filter(exit_id=x['id'], status=CaseStatus.ACTIVE)
                                    .values_list('id', 'anchor_date')):
                (LifecycleTask.objects.filter(case_id=case_id, due_date__isnull=False)
                 .update(due_date=F('due_date') + timedelta(days=(lwd - anchor).days)))
            LifecycleCase.objects.filter(exit_id=x['id'], status=CaseStatus.ACTIVE).update(anchor_date=lwd)
            own = user_id_for_employee(x['employee_id'])
            if own:
                notify(company_id=x['company_id'], user_id=own, employee_id=x['employee_id'], template='lifecycle.exit.lwd_changed',
                       title='Last working day changed', body=f'Your last working day is now {bs_label(lwd)}.', payload={'exitId': x['id']})
        audit('update', 'employee_exit', actor_id=user.id, row_id=x['id'], after=b)


def withdraw_exit(user: AuthUser, id_: str) -> None:
    from hajiri.workflow.services import cancel_workflow
    x = load_exit(user, id_)
    if x['employee_id'] != user.employee_id:
        raise ForbiddenError('only the employee can withdraw their resignation')
    if x['status'] != ExitStatus.PENDING:
        raise ConflictError('your resignation was already accepted; ask HR to cancel it' if x['status'] == ExitStatus.ACCEPTED
                            else f"resignation is {x['status']}")
    with transaction.atomic():
        EmployeeExit.objects.filter(pk=x['id']).update(status=ExitStatus.WITHDRAWN, decided_at=Now())
        cancel_workflow('resignation', x['id'])
        audit('withdraw', 'employee_exit', actor_id=user.id, row_id=x['id'])


def cancel_exit(user: AuthUser, id_: str, body: dict) -> None:
    from hajiri.workflow.services import cancel_workflow
    _require(user, 'hr')
    x = load_exit(user, id_)
    b = validate(CancelExitIn, body)
    if x['status'] not in OPEN_EXIT:
        raise ConflictError(f"exit is {x['status']}")
    with transaction.atomic():
        old = EmployeeExit.objects.select_for_update().filter(pk=x['id']).values_list('reason', flat=True).get()
        reason = '\n'.join(p for p in (old, f"Cancelled: {b['reason']}") if p is not None)    # concat_ws: a missing reason is skipped
        EmployeeExit.objects.filter(pk=x['id']).update(status=ExitStatus.CANCELLED, decided_at=Now(), reason=reason)
        cancel_workflow('resignation', x['id'])
        if x['status'] == ExitStatus.ACCEPTED:
            Employee.objects.filter(pk=x['employee_id'], exit_date=x['accepted_lwd']).update(exit_date=None, updated_at=Now())
            LifecycleCase.objects.filter(exit_id=x['id'], status=CaseStatus.ACTIVE).update(status=CaseStatus.CANCELLED, completed_at=Now())
        audit('cancel', 'employee_exit', actor_id=user.id, row_id=x['id'], after=b)


def complete(user: AuthUser, id_: str, body: dict | None = None) -> None:
    _require(user, 'hr')
    x = load_exit(user, id_)
    b = validate(CompleteExitIn, body or {})
    with transaction.atomic():
        complete_exit(x['id'], user.id, skip_open_tasks=b['skipOpenTasks'])


# Exit interview: questions are company-wide; answers are encrypted and only HR can read them.

def questions(user: AuthUser) -> list[dict]:
    qs = ExitInterviewQuestion.objects.for_company(company_id_of(user))
    if not _is_hr(user):
        qs = qs.filter(active=True)
    return [_s(r, 'id') for r in qs.order_by('sort').values('id', 'sort', 'question', 'kind', 'options', 'active')]


def save_questions(user: AuthUser, body: dict) -> None:
    _require(user, 'hr')
    company_id = company_id_of(user)
    b = plain(validate(QuestionsIn, body))
    mine = ExitInterviewQuestion.objects.for_company(company_id)
    with transaction.atomic():
        keep: list[str] = []
        for i, q in enumerate(b['questions']):
            fields = {'sort': i, 'question': q['question'], 'kind': q['kind'], 'options': q['options'], 'active': q['active']}
            if q.get('id'):
                if not mine.filter(pk=q['id']).update(**fields):
                    raise BadRequestError('question not found')
                keep.append(q['id'])
            else:
                keep.append(str(ExitInterviewQuestion.objects.create(company_id=company_id, **fields).pk))
        # Questions are never deleted (old answers refer to them); dropped ones are switched off.
        mine.exclude(pk__in=keep).update(active=False)
        audit('update', 'exit_interview_question', actor_id=user.id, after={'count': len(b['questions'])})


def _interview_aad(exit_id: str) -> str:
    return f'exit_interview:{exit_id}'


def interview(user: AuthUser, id_: str) -> dict | None:
    _require(user, 'hr')
    x = load_exit(user, id_)
    i = (ExitInterview.objects.filter(exit_id=x['id']).annotate(recorded_by_name=F('recorded_by__name'))
         .values('answers_enc', 'by_employee', 'created_at', 'recorded_by_name').first())
    if not i:
        return None
    audit('read_exit_interview', 'exit_interview', actor_id=user.id, row_id=x['id'])
    return {'answers': json.loads(field_cipher().decrypt(bytes(i['answers_enc']), _interview_aad(x['id']))), 'byEmployee': i['by_employee'],
            'createdAt': i['created_at'], 'recordedBy': i['recorded_by_name']}


def record_interview(user: AuthUser, id_: str, body: dict) -> None:
    x = load_exit(user, id_)
    own = user.employee_id == x['employee_id']
    if not own and not _is_hr(user):
        raise ForbiddenError('only the leaver or HR can record the exit interview')
    if own and not lifecycle_policy(x['company_id'])['selfExitInterview']:
        raise ForbiddenError('HR will hold your exit interview in person')
    if x['status'] not in (ExitStatus.PENDING, ExitStatus.ACCEPTED, ExitStatus.COMPLETED):
        raise ConflictError(f"exit is {x['status']}")
    b = plain(validate(InterviewIn, body))
    by_id = {str(q['id']): q for q in ExitInterviewQuestion.objects.for_company(x['company_id']).values('id', 'question', 'kind', 'options')}
    answers = []
    for a in b['answers']:
        q = by_id.get(a['questionId'])
        if not q:
            raise BadRequestError('unknown question')
        ans = a['answer']
        if q['kind'] == 'rating' and not isinstance(ans, int):
            raise BadRequestError(f'"{q["question"]}" needs a rating from 1 to 5')
        if q['kind'] == 'choice' and (not isinstance(ans, str) or ans not in (q['options'] or [])):
            raise BadRequestError(f'"{q["question"]}" needs one of the listed options')
        if q['kind'] == 'text' and not isinstance(ans, str):
            raise BadRequestError(f'"{q["question"]}" needs a written answer')
        answers.append({'questionId': str(q['id']), 'question': q['question'], 'kind': q['kind'], 'answer': ans})
    with transaction.atomic():
        try:
            with transaction.atomic():
                ExitInterview.objects.create(exit_id=x['id'], company_id=x['company_id'], by_employee=own, recorded_by_id=user.id,
                                             answers_enc=field_cipher().encrypt(json.dumps(answers, ensure_ascii=False), _interview_aad(x['id'])))
        except IntegrityError as e:
            if _unique(e):
                raise ConflictError('the exit interview has already been recorded') from e
            raise
        (LifecycleTask.objects.filter(case__exit_id=x['id'], status=TaskStatus.OPEN, title__istartswith='exit interview')
         .update(status=TaskStatus.DONE, done_by_id=user.id, done_at=Now()))
        audit('record_exit_interview', 'exit_interview', actor_id=user.id, row_id=x['id'], after={'byEmployee': own})


# ------------------------------------------------------------ assets

def asset_types(user: AuthUser) -> list[dict]:
    return [_s(r, 'id') for r in AssetType.objects.for_company(company_id_of(user)).order_by('name').values('id', 'name')]


def add_asset_type(user: AuthUser, body: dict) -> dict:
    _require(user, 'hr')
    b = validate(AssetTypeIn, body)
    try:
        with transaction.atomic():
            t = AssetType.objects.create(company_id=company_id_of(user), name=b['name'])
    except IntegrityError as e:
        if _unique(e):
            raise ConflictError('this asset type already exists') from e
        raise
    return {'id': str(t.pk), 'name': t.name}


ASSET_FIELDS = ('id', 'tag', 'name', 'serial', 'condition', 'status', 'purchase_date', 'cost', 'notes', 'type_id', 'type_name', 'created_at',
                'assignment_id', 'employee_id', 'assigned_on', 'employee_name', 'emp_code')


def asset_rows(company_id: str):
    """Assets with their type and whoever holds them now (the open assignment, a LEFT JOIN)."""
    return (Asset.objects.for_company(company_id)
            .annotate(g=FilteredRelation('asset_assignments', condition=Q(asset_assignments__returned_on__isnull=True)))
            .annotate(type_name=F('type__name'), assignment_id=F('g__id'), employee_id=F('g__employee_id'), assigned_on=F('g__assigned_on'),
                      employee_name=F('g__employee__name_en'), emp_code=F('g__employee__emp_code')))


def _asset(r):
    return _s(r, 'id', 'type_id', 'assignment_id', 'employee_id')


def assets(user: AuthUser, query: dict | None = None) -> list[dict]:
    _require(user, 'hr', 'manager')
    q = plain(validate(AssetQuery, query or {}))
    if q.get('employeeId'):
        assert_manages(user, q['employeeId'])
    elif not _is_hr(user):
        raise ForbiddenError('choose one of your team members')
    qs = AssetFilter(q, queryset=asset_rows(company_id_of(user))).qs.order_by('tag').values(*ASSET_FIELDS)
    return [_asset(r) for r in qs[:1000]]


def my_assets(user: AuthUser) -> list[dict]:
    if not user.employee_id:
        return []
    qs = asset_rows(company_id_of(user)).filter(g__employee_id=user.employee_id).order_by('g__assigned_on').values(*ASSET_FIELDS)
    return [_asset(r) for r in qs]


def asset_history(user: AuthUser, id_: str) -> list[dict]:
    _require(user, 'hr')
    return [_s(r, 'id', 'employee_id') for r in AssetAssignment.objects.for_company(company_id_of(user)).filter(asset_id=parse_uuid(str(id_)))
            .annotate(name_en=F('employee__name_en'), emp_code=F('employee__emp_code')).order_by('-assigned_on')
            .values('id', 'employee_id', 'name_en', 'emp_code', 'assigned_on', 'returned_on', 'condition_out', 'condition_in', 'notes')]


def _check_type(company_id: str, type_id) -> None:
    if type_id and not AssetType.objects.for_company(company_id).filter(pk=type_id).exists():
        raise BadRequestError('asset type not found')


ASSET_MAP = (('tag', 'tag'), ('name', 'name'), ('typeId', 'type_id'), ('serial', 'serial'), ('condition', 'condition'),
             ('purchaseDate', 'purchase_date'), ('cost', 'cost'), ('notes', 'notes'), ('status', 'status'))


def create_asset(user: AuthUser, body: dict) -> str:
    _require(user, 'hr')
    company_id = company_id_of(user)
    b = plain(validate(AssetIn, body))
    _check_type(company_id, b.get('typeId'))
    try:
        with transaction.atomic():
            aid = str(Asset.objects.create(company_id=company_id, **{col: b.get(k) for k, col in ASSET_MAP if k != 'status'}).pk)
            audit('create', 'asset', actor_id=user.id, row_id=aid, after=b)
    except IntegrityError as e:
        if _unique(e):
            raise ConflictError('an asset with this tag already exists') from e
        raise
    return aid


def update_asset(user: AuthUser, id_: str, body: dict) -> None:
    _require(user, 'hr')
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    b = plain(validate(AssetUpdateIn, body, partial=True))
    _check_type(company_id, b.get('typeId'))
    cur = Asset.objects.for_company(company_id).filter(pk=id_).values_list('status', flat=True).first()
    if not cur:
        raise NotFoundError('asset not found')
    if b.get('status') and cur == Asset.Status.ASSIGNED:
        raise ConflictError('record the return before changing the status of this asset')
    sets = {col: b[k] for k, col in ASSET_MAP if k in b}
    try:
        with transaction.atomic():
            if sets:
                Asset.objects.filter(pk=id_).update(**sets)
            audit('update', 'asset', actor_id=user.id, row_id=id_, after=b)
    except IntegrityError as e:
        if _unique(e):
            raise ConflictError('an asset with this tag already exists') from e
        raise


def assign_asset(user: AuthUser, id_: str, body: dict) -> None:
    _require(user, 'hr')
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    b = plain(validate(AssignAssetIn, body))
    e = employee_in(company_id, b['employeeId'])
    if e['status'] != Employee.Status.ACTIVE:
        raise BadRequestError('this employee has left')
    with transaction.atomic():
        asset = Asset.objects.for_company(company_id).select_for_update().filter(pk=id_).values('status', 'condition', 'name', 'tag').first()
        if not asset:
            raise NotFoundError('asset not found')
        if asset['status'] != Asset.Status.AVAILABLE:
            raise ConflictError('this asset is already with someone; record the return first' if asset['status'] == Asset.Status.ASSIGNED
                                else 'this asset is retired')
        AssetAssignment.objects.create(asset_id=id_, company_id=company_id, employee_id=b['employeeId'],
                                       assigned_on=b.get('assignedOn') or npt_today(), condition_out=asset['condition'], notes=b.get('notes'),
                                       assigned_by_id=user.id)
        Asset.objects.filter(pk=id_).update(status=Asset.Status.ASSIGNED)
        audit('assign', 'asset', actor_id=user.id, row_id=id_, after={'employeeId': b['employeeId']})
        own = user_id_for_employee(b['employeeId'])
        if own:
            notify(company_id=company_id, user_id=own, employee_id=b['employeeId'], template='lifecycle.asset.assigned',
                   title='Company asset handed to you', body=f"{asset['name']} ({asset['tag']}). Please return it when you leave.",
                   payload={'assetId': id_})


def return_asset(user: AuthUser, id_: str, body: dict) -> None:
    _require(user, 'hr')
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    b = plain(validate(ReturnAssetIn, body))
    with transaction.atomic():
        g = (AssetAssignment.objects.select_for_update(of=('self',))
             .filter(asset_id=id_, asset__company_id=company_id, returned_on__isnull=True)
             .values('id', 'assigned_on', 'employee_id').first())
        if not g:
            raise ConflictError('this asset is not with anyone')
        on = date.fromisoformat(b['returnedOn']) if b.get('returnedOn') else npt_today()
        if on < g['assigned_on']:
            raise BadRequestError('the return date cannot be before it was handed over')
        returned = {'returned_on': on, 'condition_in': b['condition'], 'returned_by_id': user.id}
        if b.get('notes') is not None:
            returned['notes'] = b['notes']                      # coalesce(new note, old note)
        AssetAssignment.objects.filter(pk=g['id']).update(**returned)
        # A lost asset leaves service; anything else is available again.
        Asset.objects.filter(pk=id_).update(status=Asset.Status.RETIRED if b['condition'] == Asset.Condition.LOST else Asset.Status.AVAILABLE,
                                            condition=b['condition'])
        audit('return', 'asset', actor_id=user.id, row_id=id_,
              after={'employeeId': str(g['employee_id']), 'condition': b['condition'], 'returnedOn': on.isoformat()})


# ------------------------------------------------------------ approval inbox summaries

def probation_summary(inst: dict) -> dict:
    x = (ProbationReview.objects.filter(pk=inst['object_id'])
         .values('rating', 'recommendation', 'extend_to', 'last_working_day', 'probation_end', 'comments').first())
    if not x:
        return {}
    if x['recommendation'] == 'confirm':
        rec = 'confirm'
    elif x['recommendation'] == 'extend':
        rec = f"extend probation to {bs_label(x['extend_to'])}"
    else:
        rec = f"end employment on {bs_label(x['last_working_day'])}"
    detail = f"Rating {x['rating']} of 5" + (f", probation ends {bs_label(x['probation_end'])}" if x['probation_end'] else '')
    return {'headline': f'Probation review: {rec}', 'detail': detail, 'note': x['comments'], **x}


def resignation_summary(inst: dict) -> dict:
    x = (EmployeeExit.objects.filter(pk=inst['object_id'])
         .values('proposed_lwd', 'accepted_lwd', 'reason', 'reason_category', 'notice_days', 'submitted_on').first())
    if not x:
        return {}
    lwd = x['accepted_lwd'] or x['proposed_lwd']
    shortfall = max(0, x['notice_days'] - diff_days(lwd, x['submitted_on']))
    return {
        'headline': f'Resignation, last working day {bs_label(lwd)}',
        'detail': f"Notice short by {shortfall} {'day' if shortfall == 1 else 'days'}" if shortfall else 'Full notice given',
        'note': ': '.join(p for p in (x['reason_category'], x['reason']) if p) or None, **x,
    }


def _card(item: dict) -> dict:
    s = item.get('summary') or {}
    extra = [s['detail']] if s.get('detail') else []
    if s.get('note'):
        extra.append(f"“{s['note']}”")
    return {'main': s.get('headline') or '', 'extra': extra}


def register() -> None:
    """Decision handlers, inbox summaries and cards for probation reviews and resignations (LifecycleConfig.ready)."""
    from hajiri.workflow.services import register_card, register_handler, register_summary
    register_handler('probation_review', svc.apply_probation_decision)
    register_handler('resignation', svc.apply_resignation_decision)
    register_summary('probation_review', probation_summary)
    register_summary('resignation', resignation_summary)
    register_card('probation_review', _card)
    register_card('resignation', _card)
