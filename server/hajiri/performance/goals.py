"""
Goals and key results with check-ins, and the KPI library (port of apps/api/src/modules/performance/goals.ts).
Bodies use the TS camelCase shape and are validated with the serializers in serializers.py.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import BooleanField, ExpressionWrapper, F, FilteredRelation, Q
from django.db.models.functions import Now

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError
from hajiri.core.scoping import as_uuid, exists_in_company
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import Employee, OrgUnit, PerfCheckin, PerfGoal, PerfKeyResult, PerfKpi
from hajiri.notifications.service import notify, user_id_for_employee

from . import serializers as s
from .common import (can_see_employee, current_assignments, direct_manager, employee_in_company, is_above_in_chain, num,
                     plain_row, plain_rows, recompute_goal_progress, round_half_up, subordinate_ids)

MEASURES = s.MEASURES
GOAL_STATUSES = s.GOAL_STATUSES
Scope, Status, Measure = PerfGoal.Scope, PerfGoal.Status, PerfKeyResult.Measure


def load_goal(company_id: str, goal_id: str) -> dict:
    r = (PerfGoal.objects.for_company(company_id).filter(pk=as_uuid(goal_id))
         .values('id', 'company_id', 'scope', 'org_unit_id', 'employee_id', 'status', 'title').first())
    if not r:
        raise NotFoundError('goal not found')
    return plain_row(r)


def _current_org_unit(employee_id: str | None) -> str | None:
    if not employee_id:
        return None
    ou = current_assignments().filter(employee_id=employee_id).values_list('org_unit_id', flat=True).first()
    return str(ou) if ou else None


def can_view_goal(user: AuthUser, g: dict) -> bool:
    """Company and department goals are visible company-wide; individual goals to the owner, their manager chain and HR."""
    if g['scope'] != Scope.INDIVIDUAL:
        return True
    return can_see_employee(user, g['employee_id'])


def can_edit_goal(user: AuthUser, g: dict) -> bool:
    """Company goals: HR. Department goals: HR or a manager in that department. Individual: owner, manager chain, HR."""
    if has_role(user, 'hr'):
        return True
    if g['scope'] == Scope.COMPANY:
        return False
    if g['scope'] == Scope.DEPARTMENT:
        return has_role(user, 'manager') and _current_org_unit(user.employee_id) == (g['org_unit_id'] and str(g['org_unit_id']))
    return user.employee_id == (g['employee_id'] and str(g['employee_id'])) or is_above_in_chain(user, g['employee_id'])


def _assert_can_edit(user: AuthUser, g: dict, what: str = 'change this goal') -> None:
    """Not visible: 404 (no probing); visible but read-only: 403."""
    if not can_view_goal(user, g):
        raise NotFoundError('goal not found')
    if not can_edit_goal(user, g):
        raise ForbiddenError(f'not allowed to {what}')


GOAL_VALUES = ('id', 'scope', 'org_unit_id', 'employee_id', 'parent_id', 'title', 'description', 'weight', 'start_date', 'due_date',
               'status', 'progress', 'created_at', 'updated_at')


def _due_nulls_last():
    return F('due_date').asc(nulls_last=True)


def _goal_rows(qs) -> list[dict]:
    """Goals with their names, key results (in order) and last check-in, in three queries."""
    goals = plain_rows(qs.values(*GOAL_VALUES, org_unit_name=F('org_unit__name_en'), employee_name=F('employee__name_en'),
                                 emp_code=F('employee__emp_code'), parent_title=F('parent__title')))
    ids = [g['id'] for g in goals]
    krs: dict[str, list] = {}
    for k in (PerfKeyResult.objects.filter(goal_id__in=ids).order_by('sort', 'created_at')
              .values('goal_id', 'id', 'title', 'measure', 'start_value', 'target_value', 'current_value', 'unit', 'weight')):
        krs.setdefault(str(k.pop('goal_id')), []).append(plain_row(k))
    last = {str(c.pop('goal_id')): c for c in PerfCheckin.objects.filter(goal_id__in=ids).order_by('goal_id', '-created_at')
            .distinct('goal_id').values('goal_id', 'created_at', 'confidence', 'comment')}
    for g in goals:
        g['key_results'] = krs.get(g['id'], [])
        g['last_checkin'] = last.get(g['id'])
    return goals


def departments(user: AuthUser) -> list[dict]:
    return plain_rows(OrgUnit.objects.for_company(company_id_of(user)).filter(kind=OrgUnit.Kind.DEPARTMENT)
                      .order_by('name_en').values('id', 'code', 'name_en'))


def colleagues(user: AuthUser, q: str | None = None) -> list[dict]:
    """Colleagues for pickers (feedback, peer reviewers): name, designation and department only."""
    q = validate(s.SearchIn, {'q': q}).get('q') if q else None
    qs = (Employee.objects.for_company(company_id_of(user)).filter(status=Employee.Status.ACTIVE)
          .annotate(a=FilteredRelation('employee_assignments', condition=Q(employee_assignments__valid__contains=npt_today()))))
    if q:
        qs = qs.filter(Q(name_en__icontains=q) | Q(emp_code__istartswith=q))
    return plain_rows(qs.order_by('name_en').values('id', 'emp_code', 'name_en', 'designation', department=F('a__org_unit__name_en'),
                                                    manager_id=F('a__manager_id'))[:500])


def list_goals(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = plain(validate(s.GoalQueryIn, query or {}))
    company_id = company_id_of(user)
    qs = PerfGoal.objects.for_company(company_id)
    if q.get('status'):
        qs = qs.filter(status=q['status'])
    if q['scope'] == 'company':
        return _goal_rows(qs.exclude(scope=Scope.INDIVIDUAL).exclude(status=Status.CANCELLED)
                          .order_by('scope', _due_nulls_last(), 'title'))
    individual = qs.filter(scope=Scope.INDIVIDUAL)
    if q['scope'] in ('mine', 'employee'):
        employee_id = user.employee_id if q['scope'] == 'mine' else q.get('employeeId')
        if not employee_id:
            return []
        employee_in_company(company_id, employee_id)
        if not can_see_employee(user, employee_id):
            raise ForbiddenError('not allowed to see these goals')
        cancelled_last = ExpressionWrapper(Q(status=Status.CANCELLED), output_field=BooleanField())
        return _goal_rows(individual.filter(employee_id=employee_id).order_by(cancelled_last, _due_nulls_last(), 'created_at'))
    if q['scope'] == 'all':
        if not has_role(user, 'hr'):
            raise ForbiddenError('requires HR')
        return _goal_rows(individual.order_by('employee__name_en', _due_nulls_last()))
    if not user.employee_id:
        if not has_role(user, 'hr'):
            return []
        return _goal_rows(individual.order_by('employee__name_en'))
    return _goal_rows(individual.filter(employee_id__in=subordinate_ids(user.employee_id))
                      .order_by('employee__name_en', _due_nulls_last()))


def goal_detail(user: AuthUser, goal_id: str) -> dict:
    company_id = company_id_of(user)
    g = load_goal(company_id, parse_uuid(goal_id))
    if not can_view_goal(user, g):
        raise NotFoundError('goal not found')
    goal = _goal_rows(PerfGoal.objects.filter(pk=g['id']))[0]
    checkins = plain_rows(PerfCheckin.objects.filter(goal_id=g['id']).order_by('-created_at')
                          .values('id', 'key_result_id', 'value', 'progress', 'confidence', 'comment', 'created_at',
                                  key_result_title=F('key_result__title'), author_name=F('author__name'))[:100])
    children = plain_rows(PerfGoal.objects.for_company(company_id).filter(parent_id=g['id']).exclude(status=Status.CANCELLED)
                          .order_by('title').values('id', 'title', 'scope', 'progress', 'status'))
    return {'goal': goal, 'checkins': checkins, 'children': children, 'canEdit': can_edit_goal(user, g)}


def _insert_key_results(goal_id: str, krs: list[dict]) -> None:
    rows = []
    for sort, k in enumerate(krs):
        boolean = k['measure'] == Measure.BOOLEAN
        cur = k.get('currentValue')
        rows.append(PerfKeyResult(
            goal_id=goal_id, title=k['title'], measure=k['measure'], start_value=0 if boolean else k['startValue'],
            target_value=1 if boolean else k['targetValue'],
            current_value=cur if cur is not None else (0 if boolean else k['startValue']),
            unit=k.get('unit'), weight=k['weight'], sort=sort))
    PerfKeyResult.objects.bulk_create(rows)


def _check_owner(user: AuthUser, company_id: str, employee_id: str | None) -> str:
    if not employee_id:
        raise BadRequestError('choose whose goal this is')
    employee_in_company(company_id, employee_id)
    if employee_id != user.employee_id and not is_above_in_chain(user, employee_id):
        raise ForbiddenError('you can set goals only for yourself and your team')
    return employee_id


def create_goal(user: AuthUser, body: dict) -> str:
    b = plain(validate(s.GoalIn, body))
    company_id = company_id_of(user)
    employee_id = org_unit_id = None
    if b['scope'] == Scope.INDIVIDUAL:
        employee_id = _check_owner(user, company_id, b.get('employeeId') or user.employee_id)
    elif b['scope'] == Scope.DEPARTMENT:
        if not b.get('orgUnitId'):
            raise BadRequestError('choose the department')
        if not exists_in_company(OrgUnit, company_id, pk=b['orgUnitId']):
            raise NotFoundError('department not found')
        org_unit_id = b['orgUnitId']
    if not can_edit_goal(user, {'scope': b['scope'], 'org_unit_id': org_unit_id, 'employee_id': employee_id}):
        raise ForbiddenError('only HR sets company goals' if b['scope'] == Scope.COMPANY else 'not allowed to set this goal')
    if b.get('parentId'):
        p = load_goal(company_id, b['parentId'])
        if not can_view_goal(user, p):
            raise NotFoundError('goal not found')
    if b.get('startDate') and b.get('dueDate') and b['dueDate'] < b['startDate']:
        raise BadRequestError('the due date is before the start date')
    with transaction.atomic():
        goal = PerfGoal.objects.create(
            company_id=company_id, scope=b['scope'], org_unit_id=org_unit_id, employee_id=employee_id, parent_id=b.get('parentId'),
            title=b['title'], description=b.get('description'), weight=b['weight'], start_date=b.get('startDate'),
            due_date=b.get('dueDate'), status=b['status'], created_by_id=user.id)
        gid = str(goal.id)
        _insert_key_results(gid, b['keyResults'])
        recompute_goal_progress(gid)
        if employee_id and employee_id != user.employee_id:
            notify(company_id=company_id, user_id=user_id_for_employee(employee_id), employee_id=employee_id, template='goal.assigned',
                   title='New goal', body=f"{user.name} set a goal for you: {b['title']}", payload={'goalId': gid})
    return gid


def _changes(b: dict, mapping: dict[str, str], blank_to_null: bool = False) -> dict:
    """The model fields to update from the body keys that were sent."""
    return {col: (None if blank_to_null and b[k] == '' else b[k]) for k, col in mapping.items() if k in b}


def _has_key_results(goal_id: str) -> bool:
    return PerfKeyResult.objects.filter(goal_id=goal_id).exists()


def update_goal(user: AuthUser, goal_id: str, body: dict) -> None:
    b = plain(validate(s.GoalPatch, body, partial=True))
    company_id = company_id_of(user)
    g = load_goal(company_id, parse_uuid(goal_id))
    _assert_can_edit(user, g)
    if b.get('parentId'):
        if b['parentId'] == g['id']:
            raise BadRequestError('a goal cannot be aligned to itself')
        load_goal(company_id, b['parentId'])
    if 'progress' in b and _has_key_results(g['id']):
        raise BadRequestError('progress comes from the key results; update those instead')
    changes = _changes(b, {'title': 'title', 'description': 'description', 'weight': 'weight', 'startDate': 'start_date',
                           'dueDate': 'due_date', 'status': 'status', 'progress': 'progress', 'parentId': 'parent_id'})
    if changes:
        PerfGoal.objects.filter(pk=g['id']).update(**changes, updated_at=Now())


def cancel_goal(user: AuthUser, goal_id: str) -> None:
    g = load_goal(company_id_of(user), parse_uuid(goal_id))
    _assert_can_edit(user, g)
    with transaction.atomic():
        PerfGoal.objects.filter(pk=g['id']).update(status=Status.CANCELLED, updated_at=Now())
        audit('cancel', 'perf_goal', actor_id=user.id, row_id=g['id'], before={'title': g['title'], 'status': g['status']})


def add_key_result(user: AuthUser, goal_id: str, body: dict) -> None:
    b = validate(s.KeyResultIn, body)
    g = load_goal(company_id_of(user), parse_uuid(goal_id))
    _assert_can_edit(user, g)
    with transaction.atomic():
        _insert_key_results(g['id'], [b])
        recompute_goal_progress(g['id'])


def _kr_goal(company_id: str, kr_id: str) -> dict:
    goal_id = PerfKeyResult.objects.for_company(company_id).filter(pk=kr_id).values_list('goal_id', flat=True).first()
    if not goal_id:
        raise NotFoundError('key result not found')
    return load_goal(company_id, str(goal_id))


def update_key_result(user: AuthUser, kr_id: str, body: dict) -> None:
    b = validate(s.KeyResultIn, body, partial=True)
    kr_id = parse_uuid(kr_id)
    goal = _kr_goal(company_id_of(user), kr_id)
    _assert_can_edit(user, goal)
    changes = _changes(b, {'title': 'title', 'measure': 'measure', 'startValue': 'start_value', 'targetValue': 'target_value',
                           'currentValue': 'current_value', 'unit': 'unit', 'weight': 'weight'})
    with transaction.atomic():
        if changes:
            PerfKeyResult.objects.filter(pk=kr_id).update(**changes)
        recompute_goal_progress(goal['id'])


def delete_key_result(user: AuthUser, kr_id: str) -> None:
    kr_id = parse_uuid(kr_id)
    goal = _kr_goal(company_id_of(user), kr_id)
    _assert_can_edit(user, goal)
    with transaction.atomic():
        PerfKeyResult.objects.filter(pk=kr_id).delete()
        recompute_goal_progress(goal['id'])


def checkin(user: AuthUser, goal_id: str, body: dict) -> None:
    b = plain(validate(s.CheckinIn, body))
    company_id = company_id_of(user)
    g = load_goal(company_id, parse_uuid(goal_id))
    _assert_can_edit(user, g, 'check in on this goal')
    if g['status'] == Status.CANCELLED:
        raise BadRequestError('this goal was cancelled')
    if 'value' in b and not b.get('keyResultId'):
        raise BadRequestError('choose the key result the value belongs to')
    with transaction.atomic():
        if b.get('keyResultId'):
            kr = PerfKeyResult.objects.filter(pk=b['keyResultId'], goal_id=g['id'])
            if not kr.exists():
                raise NotFoundError('key result not found')
            if 'value' in b:
                kr.update(current_value=b['value'])
            recompute_goal_progress(g['id'])
        elif 'progress' in b:
            if _has_key_results(g['id']):
                raise BadRequestError('progress comes from the key results; update those instead')
            PerfGoal.objects.filter(pk=g['id']).update(progress=b['progress'], updated_at=Now())
        progress = PerfGoal.objects.filter(pk=g['id']).values_list('progress', flat=True).get()
        PerfCheckin.objects.create(goal_id=g['id'], key_result_id=b.get('keyResultId'), author_id=user.id, value=b.get('value'),
                                   progress=progress, confidence=b.get('confidence'), comment=b.get('comment'))
        # The other side of the conversation hears about it: the owner's manager, or the owner when someone else checked in.
        if g['scope'] == Scope.INDIVIDUAL and g['employee_id']:
            target = direct_manager(g['employee_id']) if g['employee_id'] == user.employee_id else g['employee_id']
            if target:
                comment = b.get('comment')
                notify(company_id=company_id, user_id=user_id_for_employee(target), employee_id=target, template='goal.checkin',
                       title='Goal update',
                       body=f'{user.name} updated "{g["title"]}" ({round_half_up(num(progress))}%)' + (f': {comment[:140]}' if comment else ''),
                       payload={'goalId': g['id']})


# ---------------------------------------------------------------- KPI library

KPI_VALUES = ('id', 'designation', 'title', 'description', 'measure', 'default_target', 'unit', 'active')


def list_kpis(user: AuthUser, designation: str | None = None) -> list[dict]:
    d = validate(s.DesignationIn, {'designation': designation}).get('designation') if designation else None
    qs = PerfKpi.objects.for_company(company_id_of(user))
    if not has_role(user, 'hr'):
        qs = qs.filter(active=True)
    if d:
        qs = qs.filter(Q(designation__isnull=True) | Q(designation__iexact=d))
    return plain_rows(qs.order_by(F('designation').asc(nulls_first=True), 'title').values(*KPI_VALUES))


def get_kpi(user: AuthUser, kpi_id: str) -> dict:
    """One KPI of the library for the edit form (inactive ones for HR only, like the list)."""
    qs = PerfKpi.objects.for_company(company_id_of(user)).filter(pk=as_uuid(kpi_id))
    if not has_role(user, 'hr'):
        qs = qs.filter(active=True)
    k = plain_row(qs.values(*KPI_VALUES).first())
    if not k:
        raise NotFoundError('KPI not found')
    return k


def _require_hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


def create_kpi(user: AuthUser, body: dict) -> str:
    _require_hr(user)
    b = validate(s.KpiIn, body)
    k = PerfKpi.objects.create(company_id=company_id_of(user), designation=b.get('designation') or None, title=b['title'],
                               description=b.get('description'), measure=b['measure'], default_target=b['defaultTarget'],
                               unit=b.get('unit'), active=b['active'])
    return str(k.id)


def update_kpi(user: AuthUser, kpi_id: str, body: dict) -> None:
    _require_hr(user)
    b = validate(s.KpiIn, body, partial=True)
    kpi_id = parse_uuid(kpi_id)
    changes = _changes(b, {'designation': 'designation', 'title': 'title', 'description': 'description', 'measure': 'measure',
                           'defaultTarget': 'default_target', 'unit': 'unit', 'active': 'active'}, blank_to_null=True)
    if changes and not PerfKpi.objects.for_company(company_id_of(user)).filter(pk=kpi_id).update(**changes):
        raise NotFoundError('KPI not found')


def delete_kpi(user: AuthUser, kpi_id: str) -> None:
    _require_hr(user)
    if not PerfKpi.objects.for_company(company_id_of(user)).filter(pk=parse_uuid(kpi_id)).update(active=False):
        raise NotFoundError('KPI not found')


def apply_kpi(user: AuthUser, kpi_id: str, body: dict) -> str:
    """Turn a library KPI into an individual goal with one key result."""
    b = plain(validate(s.ApplyKpiIn, body))
    company_id = company_id_of(user)
    kpi = PerfKpi.objects.for_company(company_id).filter(pk=parse_uuid(kpi_id), active=True).first()
    if not kpi:
        raise NotFoundError('KPI not found')
    employee_id = _check_owner(user, company_id, b.get('employeeId') or user.employee_id)
    with transaction.atomic():
        goal = PerfGoal.objects.create(company_id=company_id, scope=Scope.INDIVIDUAL, employee_id=employee_id, title=kpi.title,
                                       description=kpi.description, weight=b['weight'], due_date=b.get('dueDate'),
                                       created_by_id=user.id)
        _insert_key_results(str(goal.id), [{'title': kpi.title, 'measure': kpi.measure, 'startValue': 0,
                                            'targetValue': b['target'] if 'target' in b else kpi.default_target,
                                            'unit': kpi.unit, 'weight': 1}])
    return str(goal.id)
