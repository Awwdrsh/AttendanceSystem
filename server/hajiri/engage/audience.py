"""
Audiences and placement (port of apps/api/src/modules/engage/service.ts, first half). Used by engage and by relations
(popup notices): `placement_of` and `audience_sql` are the public interface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hajiri.core.auth import AuthUser, has_role
from hajiri.core.db import one, rows

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


def placement_of(employee_id: str | None) -> Placement:
    if not employee_id:
        return Placement()
    r = one("""
        WITH a AS (
          SELECT branch_id, org_unit_id, manager_id FROM employee_assignment
           WHERE employee_id = %s ORDER BY (valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date) DESC, lower(valid) DESC LIMIT 1)
        SELECT a.branch_id, a.org_unit_id, a.manager_id,
               (WITH RECURSIVE up AS (SELECT id, parent_id FROM org_unit WHERE id = a.org_unit_id
                                      UNION SELECT o.id, o.parent_id FROM org_unit o JOIN up ON o.id = up.parent_id)
                SELECT array_agg(id) FROM up) AS units
          FROM a""", [employee_id])
    if not r:
        return Placement()
    return Placement(branch_id=_s(r['branch_id']), unit_ids=[str(u) for u in (r['units'] or [])],
                     manager_id=_s(r['manager_id']), org_unit_id=_s(r['org_unit_id']))


def audience_sql(alias: str, p: Placement, params: list, with_reports: bool) -> str:
    """
    SQL condition true when the item (alias with audience_kind / audience_id, and author_employee_id when
    `with_reports`) targets the person at placement `p`. Three values are appended to `params`, consumed by the three
    `%s` placeholders of the returned fragment in order, so put the fragment where those values sit in the statement
    (after every placeholder whose value was appended before the call, before any appended after it).
    """
    params.extend([p.branch_id, list(p.unit_ids), p.manager_id])
    reports = (f"OR ({alias}.audience_kind = 'reports' AND {alias}.author_employee_id = %s::uuid)" if with_reports
               else 'OR (%s::uuid IS NOT NULL AND false)')
    return (f"({alias}.audience_kind = 'all'\n"
            f"     OR ({alias}.audience_kind = 'branch' AND {alias}.audience_id = %s::uuid)\n"
            f"     OR ({alias}.audience_kind = 'department' AND {alias}.audience_id = ANY(%s::uuid[]))\n"
            f"     {reports})")


def audience_members(company_id: str, kind: str, audience_id: str | None, author_employee_id: str | None) -> list[dict]:
    """Active employees (with their active user account, if any) an audience reaches:
    [{'employee_id', 'user_id', 'org_unit_id', 'branch_id'}] with str ids."""
    out = rows("""
        WITH RECURSIVE units AS (
            SELECT id FROM org_unit WHERE id = %(aud)s::uuid AND company_id = %(co)s
            UNION SELECT o.id FROM org_unit o JOIN units ON o.parent_id = units.id),
          cur AS (
            SELECT DISTINCT ON (a.employee_id) a.employee_id, a.branch_id, a.org_unit_id, a.manager_id
              FROM employee_assignment a JOIN employee e ON e.id = a.employee_id AND e.company_id = %(co)s
             ORDER BY a.employee_id, (a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date) DESC, lower(a.valid) DESC)
        SELECT e.id AS employee_id, u.id AS user_id, cur.org_unit_id, cur.branch_id
          FROM employee e
          LEFT JOIN cur ON cur.employee_id = e.id
          LEFT JOIN app_user u ON u.employee_id = e.id AND u.status = 'active'
         WHERE e.company_id = %(co)s AND e.status = 'active'
           AND CASE %(kind)s::text
                 WHEN 'all' THEN true
                 WHEN 'branch' THEN cur.branch_id IS NOT DISTINCT FROM %(aud)s::uuid
                 WHEN 'department' THEN cur.org_unit_id IN (SELECT id FROM units)
                 WHEN 'reports' THEN cur.manager_id IS NOT DISTINCT FROM %(author)s::uuid AND %(author)s::uuid IS NOT NULL
                 ELSE false END""",
               {'co': company_id, 'kind': kind, 'aud': audience_id, 'author': author_employee_id})
    return [{k: _s(v) for k, v in r.items()} for r in out]


def audience_target_exists(company_id: str, kind: str, id_: str | None) -> bool:
    """The audience target (branch or org unit) must belong to the company."""
    if kind in ('all', 'reports'):
        return True
    if not id_:
        return False
    table = 'branch' if kind == 'branch' else 'org_unit'
    return one(f'SELECT 1 AS x FROM {table} WHERE id = %s AND company_id = %s', [id_, company_id]) is not None


def is_hr(user: AuthUser) -> bool:
    return has_role(user, 'hr')
