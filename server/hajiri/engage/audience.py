"""
Audiences and placement (port of apps/api/src/modules/engage/service.ts, first half). Used by engage and by relations
(popup notices): `placement_of` and `audience_q` are the public interface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.db.models import BooleanField, DateField, ExpressionWrapper, F, Func, OuterRef, Q, Subquery

from hajiri.core.auth import AuthUser, has_role
from hajiri.core.clock import npt_today
from hajiri.db.models import AppUser, Branch, CompanyMembership, Employee, EmployeeAssignment, OrgUnit

AUDIENCE_KINDS = ('all', 'branch', 'department', 'reports')


@dataclass(frozen=True)
class Placement:
    """Where someone sits today: branch, org unit with all its parent units, and line manager."""
    branch_id: str | None = None
    unit_ids: list[str] = field(default_factory=list)
    manager_id: str | None = None
    org_unit_id: str | None = None


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def current_assignments():
    """Assignments with the one in force today first, then the latest start: an employee's current assignment is the
    first row of this order."""
    return (EmployeeAssignment.objects
            .annotate(in_force=ExpressionWrapper(Q(valid__contains=npt_today()), output_field=BooleanField()),
                      starts=Func(F('valid'), function='lower', output_field=DateField()))
            .order_by(F('in_force').desc(), F('starts').desc()))


def _unit_tree(company_id) -> dict[str, str | None]:
    """{org unit id: parent id} of one company. A company has a few dozen units, so the tree is walked in Python
    instead of with a recursive CTE."""
    if not company_id:
        return {}
    return {str(i): _s(p) for i, p in OrgUnit.objects.for_company(company_id).values_list('id', 'parent_id')}


def _ancestors(tree: dict[str, str | None], unit_id: str | None) -> list[str]:
    """The unit and every unit above it."""
    out: list[str] = []
    u = unit_id
    while u and u in tree and u not in out:
        out.append(u)
        u = tree[u]
    return out


def _descendants(tree: dict[str, str | None], unit_id: str | None) -> set[str]:
    """The unit and every unit below it."""
    if not unit_id or unit_id not in tree:
        return set()
    children: dict[str, list[str]] = {}
    for u, p in tree.items():
        if p:
            children.setdefault(p, []).append(u)
    out: set[str] = set()
    todo = [unit_id]
    while todo:
        u = todo.pop()
        if u not in out:
            out.add(u)
            todo += children.get(u, [])
    return out


def placement_of(employee_id: str | None) -> Placement:
    if not employee_id:
        return Placement()
    a = (current_assignments().filter(employee_id=employee_id)
         .values('branch_id', 'org_unit_id', 'manager_id', 'employee__company_id').first())
    if not a:
        return Placement()
    unit = _s(a['org_unit_id'])
    units = _ancestors(_unit_tree(a['employee__company_id']), unit) if unit else []
    return Placement(branch_id=_s(a['branch_id']), unit_ids=units, manager_id=_s(a['manager_id']), org_unit_id=unit)


def audience_q(p: Placement, with_reports: bool, prefix: str = '') -> Q:
    """
    True when the item (with audience_kind / audience_id, and author_employee_id when `with_reports`) targets the
    person at placement `p`. `prefix` is the lookup path to the item ('' for the model itself, 'announcement__', ...).
    """
    kind, aud = f'{prefix}audience_kind', f'{prefix}audience_id'
    q = Q(**{kind: 'all'})
    if p.branch_id:
        q |= Q(**{kind: 'branch', aud: p.branch_id})
    if p.unit_ids:
        q |= Q(**{kind: 'department', f'{aud}__in': list(p.unit_ids)})
    if with_reports and p.manager_id:
        q |= Q(**{kind: 'reports', f'{prefix}author_employee_id': p.manager_id})
    return q


def accounts_of(employee_ids) -> dict[str, str]:
    """{employee id: user id} of the active account working as each employee: the home account
    (app_user.employee_id), or a member of the group whose membership in that company is this employee record."""
    ids = list(dict.fromkeys(str(e) for e in employee_ids if e))
    if not ids:
        return {}
    out = {str(e): str(u) for e, u in CompanyMembership.objects.filter(
        employee_id__in=ids, status=CompanyMembership.Status.ACTIVE, user__status=AppUser.Status.ACTIVE,
    ).values_list('employee_id', 'user_id')}
    out.update({str(e): str(u) for e, u in AppUser.objects.filter(
        employee_id__in=ids, status=AppUser.Status.ACTIVE).values_list('employee_id', 'id')})
    return out


def audience_members(company_id: str, kind: str, audience_id: str | None, author_employee_id: str | None) -> list[dict]:
    """Active employees (with their active user account, if any) an audience reaches:
    [{'employee_id', 'user_id', 'org_unit_id', 'branch_id'}] with str ids."""
    if kind not in AUDIENCE_KINDS or (kind == 'reports' and not author_employee_id):
        return []
    cur = current_assignments().filter(employee_id=OuterRef('pk'))
    qs = (Employee.objects.for_company(company_id).filter(status=Employee.Status.ACTIVE)
          .annotate(cur_branch=Subquery(cur.values('branch_id')[:1]), cur_unit=Subquery(cur.values('org_unit_id')[:1]),
                    cur_manager=Subquery(cur.values('manager_id')[:1])))
    if kind == 'branch':
        qs = qs.filter(cur_branch=audience_id) if audience_id else qs.filter(cur_branch__isnull=True)
    elif kind == 'department':
        qs = qs.filter(cur_unit__in=_descendants(_unit_tree(company_id), _s(audience_id)))
    elif kind == 'reports':
        qs = qs.filter(cur_manager=author_employee_id)
    found = [(str(r['id']), _s(r['cur_unit']), _s(r['cur_branch']))
             for r in qs.order_by('id').values('id', 'cur_unit', 'cur_branch')]
    users = accounts_of(e for e, _, _ in found)
    return [{'employee_id': e, 'user_id': users.get(e), 'org_unit_id': u, 'branch_id': b} for e, u, b in found]


def audience_target_exists(company_id: str, kind: str, id_: str | None) -> bool:
    """The audience target (branch or org unit) must belong to the company."""
    if kind in ('all', 'reports'):
        return True
    if not id_:
        return False
    model = Branch if kind == 'branch' else OrgUnit
    return model.objects.for_company(company_id).filter(pk=id_).exists()


def is_hr(user: AuthUser) -> bool:
    return has_role(user, 'hr')
