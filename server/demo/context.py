"""Everything a section needs (port of apps/api/src/db/demo-suite/context.ts)."""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Callable

from hajiri.core.auth import create_user
from hajiri.core.clock import npt_today
from hajiri.core.db import execute, one, rows

from .kit import Actor, Kit


@dataclass
class DemoEmployee:
    id: str
    code: str
    name: str
    first: str
    last: str
    gender: str | None
    designation: str | None
    join_date: str | None
    org_unit_id: str | None
    org_code: str | None
    manager_id: str | None
    branch_id: str | None
    user_id: str | None


@dataclass
class DemoContext:
    """The company, today, the employees in code order, and sessions for HR and any employee."""
    kit: Kit
    company_id: str
    company_name: str
    today: str
    domain: str
    hr: Actor
    employees: list[DemoEmployee]
    log: Callable[[str], None]
    _sessions: dict[str, Actor] = field(default_factory=dict)

    def by_code(self, code: str) -> DemoEmployee:
        for e in self.employees:
            if e.code == code:
                return e
        raise RuntimeError(f'no employee {code}')

    def as_(self, e: DemoEmployee) -> Actor:
        if not e.user_id:
            raise RuntimeError(f'{e.code} has no account')
        hit = self._sessions.get(e.user_id)
        if hit:
            return hit
        a = self.kit.actor(e.user_id)
        self._sessions[e.user_id] = a
        return a

    def manager_of(self, e: DemoEmployee) -> Actor:
        """The manager's session for an employee (falls back to HR when there is no line manager with an account)."""
        m = next((x for x in self.employees if x.id == e.manager_id), None) if e.manager_id else None
        return self.as_(m) if m and m.user_id else self.hr


def already_seeded(ctx: DemoContext, section: str, done_sql: str, started_sql: str) -> bool:
    """True when a section's data is already there (its last record exists), so the section is skipped. When only its
    first records exist, an earlier run stopped half-way: refuse rather than add duplicates."""
    if rows(done_sql, [ctx.company_id]):
        return True
    if rows(started_sql, [ctx.company_id]):
        raise RuntimeError(f'the {section} data is only partly there from an earlier run; restore the demo database '
                           '(or remove that data) and run again')
    return False


def _s(v) -> str | None:
    return None if v is None else str(v)


def _iso(v) -> str | None:
    return None if v is None else (v if isinstance(v, str) else v.isoformat())


def build_context(kit: Kit, company_code: str, log: Callable[[str], None]) -> DemoContext:
    c = one('SELECT id, name_en FROM company WHERE code = %s', [company_code])
    if not c:
        raise RuntimeError(f'company {company_code} not found')
    company_id = str(c['id'])
    today = npt_today().isoformat()
    hr_row = one("""SELECT id, email FROM app_user WHERE company_id = %s AND status = 'active' AND (roles && ARRAY['hr', 'admin']::text[])
                      AND NOT must_change_password
                    ORDER BY 'hr' = ANY(roles) DESC, created_at LIMIT 1""", [company_id])
    if not hr_row:
        raise RuntimeError('the company needs an active HR or admin user (who has set their password) to run the demo suite')
    parts = hr_row['email'].split('@')
    domain = parts[1] if len(parts) > 1 else 'example.com'

    def load() -> list[dict]:
        return rows("""
            SELECT e.id, e.emp_code, e.name_en, e.gender, e.designation, e.join_date, a.org_unit_id, o.code AS org_code, a.manager_id, a.branch_id,
                   (SELECT u.id FROM app_user u WHERE u.employee_id = e.id AND u.status = 'active' ORDER BY u.created_at LIMIT 1) AS user_id
              FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> %s::date LEFT JOIN org_unit o ON o.id = a.org_unit_id
             WHERE e.company_id = %s AND e.status = 'active' ORDER BY e.emp_code""", [today, company_id])

    found = load()
    if len(found) < 8:
        raise RuntimeError(f'the demo suite needs at least 8 active employees (found {len(found)})')

    # Every employee gets an account so they can raise requests; the password is random (HR can reset it to hand it over).
    managers = {str(r['manager_id']) for r in found if r['manager_id']}
    created = 0
    for r in found:
        rid = str(r['id'])
        want_roles = ['employee', 'manager'] if rid in managers else ['employee']
        if r['user_id']:
            if rid in managers:
                execute("""UPDATE app_user SET roles = array(SELECT DISTINCT unnest(roles || ARRAY['manager']::text[]))
                            WHERE id = %s AND NOT ('manager' = ANY(roles))""", [r['user_id']])
            continue
        first = re.sub(r'[^a-z]', '', re.split(r'\s+', r['name_en'])[0].lower()) or 'user'
        email = f'{first}@{domain}'
        if one('SELECT 1 AS x FROM app_user WHERE email = %s', [email]):
            email = f"{first}.{r['emp_code'].lower()}@{domain}"
        create_user(company_id=company_id, employee_id=rid, email=email, name=r['name_en'], password=secrets.token_urlsafe(18),
                    roles=want_roles, must_change_password=False)
        created += 1
    if created:
        log(f'created {created} employee accounts (random passwords; reset one with the CLI to sign in as that person)')
        found = load()

    employees = []
    for r in found:
        names = re.split(r'\s+', r['name_en'])
        employees.append(DemoEmployee(
            id=str(r['id']), code=r['emp_code'], name=r['name_en'], first=names[0], last=' '.join(names[1:]) or names[0],
            gender=r['gender'], designation=r['designation'], join_date=_iso(r['join_date']), org_unit_id=_s(r['org_unit_id']),
            org_code=r['org_code'], manager_id=_s(r['manager_id']), branch_id=_s(r['branch_id']), user_id=_s(r['user_id'])))
    hr = kit.actor(str(hr_row['id']))
    return DemoContext(kit=kit, company_id=company_id, company_name=c['name_en'], today=today, domain=domain, hr=hr,
                       employees=employees, log=log)
