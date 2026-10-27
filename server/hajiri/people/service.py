"""
People and core HR (port of apps/api/src/modules/people/routes.ts): org structure, directory and org chart for
everyone, the full employee profile (self and HR), career events, profile change requests and field-level history.
Every function takes the signed-in AuthUser and a body in the TS camelCase shape, and raises hajiri.core.errors.
"""
from __future__ import annotations

import json
from django.db import IntegrityError, transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role, is_manager_of
from hajiri.core.bsdate import bs_label
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from copy import copy

from hajiri.core.schema import MISSING, Any_, Bool, Enum, Int, Obj, Str, Uuid, parse, parse_uuid

from . import career as career_mod
from .change_requests import PROFILE_CHANGE, REQ_COLS, create_change_request, decrypt_payload, req_row
from .profile import SECRET_FIELDS, SECTION_LABEL, SENSITIVE_SECTIONS, apply_section, diff_section, parse_section, read_profile

PEOPLE_POLICY = 'people.policy'
POLICY_SCHEMA = Obj({'makerChecker': Bool(default=False)}, strict=True)


def people_policy(company_id: str) -> dict:
    r = one('SELECT value FROM company_setting WHERE company_id = %s AND key = %s', [company_id, PEOPLE_POLICY])
    return parse(POLICY_SCHEMA, (r and r['value']) or {})


def like(s: str) -> str:
    """Search text is matched literally."""
    return '%' + s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def _sid(r: dict | None, *keys: str) -> dict | None:
    if r is None:
        return None
    for k in keys:
        if r.get(k) is not None:
            r[k] = str(r[k])
    return r


def employee_in(company_id: str, id_: str) -> dict:
    """Same company, else 404 (never reveal other companies' ids)."""
    e = one('SELECT id, name_en, status, avatar_file_id FROM employee WHERE id = %s AND company_id = %s', [id_, company_id])
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


# ---------------------------------------------------------------- directory, summary, org chart (everyone)

DIRECTORY_COLS = """e.id, e.emp_code, e.name_en, e.designation, e.email AS work_email, e.work_phone, e.join_date,
    e.avatar_file_id IS NOT NULL AS has_avatar,
    a.branch_id, b.name_en AS branch_name, a.org_unit_id, o.name_en AS unit_name, a.manager_id, m.name_en AS manager_name,
    (SELECT json_build_object('kind', t.kind, 'label', coalesce(t.designation, tb.name_en, tu.name_en), 'until', t.end_date)
       FROM employee_temp_assignment t LEFT JOIN branch tb ON tb.id = t.branch_id LEFT JOIN org_unit tu ON tu.id = t.org_unit_id
      WHERE t.employee_id = e.id AND t.status = 'active' AND (now() AT TIME ZONE 'Asia/Kathmandu')::date BETWEEN t.start_date AND t.end_date
      ORDER BY t.start_date DESC LIMIT 1) AS temp_assignment"""
DIRECTORY_FROM = """employee e
    LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
    LEFT JOIN branch b ON b.id = a.branch_id LEFT JOIN org_unit o ON o.id = a.org_unit_id LEFT JOIN employee m ON m.id = a.manager_id"""
_DIR_IDS = ('id', 'branch_id', 'org_unit_id', 'manager_id')

DIRECTORY_QUERY = Obj({'search': Str(trim=True, max=100, optional=True), 'orgUnitId': Uuid(optional=True), 'branchId': Uuid(optional=True)})


def directory(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = parse(DIRECTORY_QUERY, query or {})
    search = q.get('search')
    return [_sid(r, *_DIR_IDS) for r in rows(
        f"""SELECT {DIRECTORY_COLS} FROM {DIRECTORY_FROM}
             WHERE e.company_id = %s AND e.status = 'active'
               AND (%s::text IS NULL OR e.name_en ILIKE %s OR e.emp_code ILIKE %s OR e.designation ILIKE %s OR o.name_en ILIKE %s
                    OR array_to_string(e.skills, ' ') ILIKE %s)
               AND (%s::uuid IS NULL OR a.org_unit_id = %s) AND (%s::uuid IS NULL OR a.branch_id = %s)
             ORDER BY e.name_en LIMIT 1000""",
        [company_id_of(user), *([like(search) if search else None] * 6), q.get('orgUnitId'), q.get('orgUnitId'),
         q.get('branchId'), q.get('branchId')])]


def summary(user: AuthUser) -> dict:
    company_id = company_id_of(user)
    t = one("""SELECT count(*) FILTER (WHERE e.status = 'active')::int AS active,
                      count(*) FILTER (WHERE e.status = 'active' AND e.join_date > (now() AT TIME ZONE 'Asia/Kathmandu')::date - 30)::int AS joined_30d,
                      count(*) FILTER (WHERE e.status = 'active' AND e.confirmed_on IS NULL AND coalesce(a.probation_end, e.probation_end) >= (now() AT TIME ZONE 'Asia/Kathmandu')::date)::int AS on_probation,
                      count(*) FILTER (WHERE e.status = 'active' AND e.exit_date >= (now() AT TIME ZONE 'Asia/Kathmandu')::date)::int AS leaving
                 FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date WHERE e.company_id = %s""",
            [company_id])
    by_unit = [_sid(r, 'id') for r in rows(
        """SELECT o.id, o.name_en AS name, count(e.id)::int AS count FROM org_unit o
             LEFT JOIN employee_assignment a ON a.org_unit_id = o.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
             LEFT JOIN employee e ON e.id = a.employee_id AND e.status = 'active'
            WHERE o.company_id = %s GROUP BY o.id, o.name_en ORDER BY count(e.id) DESC, o.name_en""", [company_id])]
    by_branch = [_sid(r, 'id') for r in rows(
        """SELECT b.id, b.name_en AS name, count(e.id)::int AS count FROM branch b
             LEFT JOIN employee_assignment a ON a.branch_id = b.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
             LEFT JOIN employee e ON e.id = a.employee_id AND e.status = 'active'
            WHERE b.company_id = %s GROUP BY b.id, b.name_en ORDER BY count(e.id) DESC, b.name_en""", [company_id])]
    return {'active': t['active'], 'joinedLast30Days': t['joined_30d'], 'onProbation': t['on_probation'], 'leaving': t['leaving'],
            'byUnit': by_unit, 'byBranch': by_branch}


def org_chart(user: AuthUser) -> list[dict]:
    return [_sid(r, 'id', 'manager_id') for r in rows(
        """SELECT e.id, e.name_en, e.designation, e.avatar_file_id IS NOT NULL AS has_avatar, o.name_en AS unit_name,
                  CASE WHEN m.status = 'active' THEN a.manager_id END AS manager_id
             FROM employee e
             LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
             LEFT JOIN org_unit o ON o.id = a.org_unit_id LEFT JOIN employee m ON m.id = a.manager_id
            WHERE e.company_id = %s AND e.status = 'active' ORDER BY e.name_en""", [company_id_of(user)])]


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
    roots.sort(key=lambda r: (-size.get(r['id'], 0), r['name_en'].lower()))
    return {'roots': roots, 'children': children, 'parent': parent, 'size': size}


# ---------------------------------------------------------------- org structure

def structure(user: AuthUser) -> dict:
    company_id = company_id_of(user)
    branches = [_sid(r, 'id', 'head_employee_id') for r in rows(
        """SELECT b.id, b.code, b.name_en, b.address, b.phone, b.head_employee_id, h.name_en AS head_name,
                  (SELECT count(*)::int FROM employee_assignment a JOIN employee e ON e.id = a.employee_id AND e.status = 'active'
                    WHERE a.branch_id = b.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date) AS headcount
             FROM branch b LEFT JOIN employee h ON h.id = b.head_employee_id WHERE b.company_id = %s ORDER BY b.code""", [company_id])]
    units = [_sid(r, 'id', 'parent_id', 'branch_id', 'head_employee_id') for r in rows(
        """SELECT o.id, o.code, o.name_en, o.kind, o.parent_id, o.branch_id, o.description, o.head_employee_id, h.name_en AS head_name,
                  (SELECT count(*)::int FROM employee_assignment a JOIN employee e ON e.id = a.employee_id AND e.status = 'active'
                    WHERE a.org_unit_id = o.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date) AS headcount
             FROM org_unit o LEFT JOIN employee h ON h.id = o.head_employee_id WHERE o.company_id = %s ORDER BY o.name_en""", [company_id])]
    designations = [_sid(r, 'id', 'org_unit_id') for r in rows(
        """SELECT d.id, d.code, d.name_en, d.grade, d.job_level, d.org_unit_id, d.description, d.active,
                  (SELECT count(*)::int FROM employee e WHERE e.designation_id = d.id AND e.status = 'active') AS headcount
             FROM designation d WHERE d.company_id = %s ORDER BY d.job_level NULLS LAST, d.name_en""", [company_id])]
    return {'branches': branches, 'units': units, 'designations': designations}


def _owned_or_null(company_id: str, table: str, id_, label: str) -> None:
    if not id_:
        return
    if not one(f'SELECT 1 AS x FROM {table} WHERE id = %s AND company_id = %s', [id_, company_id]):
        raise BadRequestError(f'{label} does not exist in your company')


def _unique_violation(e: Exception) -> bool:
    return getattr(getattr(e, '__cause__', None), 'sqlstate', None) == '23505'


def _fk_violation(e: Exception) -> bool:
    return getattr(getattr(e, '__cause__', None), 'sqlstate', None) == '23503'


def _dup(what: str):
    return ConflictError(f'another {what} already uses this code')


BRANCH_BODY = Obj({
    'code': Str(trim=True, min=1, max=20), 'nameEn': Str(trim=True, min=1, max=120),
    'address': Str(trim=True, max=300, nullable=True, optional=True), 'phone': Str(trim=True, max=30, nullable=True, optional=True),
    'headEmployeeId': Uuid(nullable=True, optional=True),
}, strict=True)


def _partial(o: Obj) -> Obj:
    """zod .partial(): every key may be left out."""
    fields = {}
    for k, f in o.fields.items():
        f2 = copy(f)
        f2.optional = True
        f2.default = MISSING
        fields[k] = f2
    return Obj(fields, strict=o.strict)


def create_branch(user: AuthUser, body: dict) -> dict:
    _hr(user)
    company_id = company_id_of(user)
    b = parse(BRANCH_BODY, body)
    _owned_or_null(company_id, 'employee', b.get('headEmployeeId'), 'branch head')
    try:
        with transaction.atomic():
            row = _sid(one('INSERT INTO branch (company_id, code, name_en, address, phone, head_employee_id) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, code, name_en',
                           [company_id, b['code'], b['nameEn'], b.get('address'), b.get('phone'), b.get('headEmployeeId')]), 'id')
            audit('create', 'branch', actor_id=user.id, row_id=row['id'], after=b)
    except IntegrityError as e:
        if _unique_violation(e):
            raise _dup('branch') from e
        raise
    return row


def update_branch(user: AuthUser, id_: str, body: dict) -> dict:
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    b = parse(_partial(BRANCH_BODY), body)
    _owned_or_null(company_id, 'employee', b.get('headEmployeeId'), 'branch head')
    try:
        with transaction.atomic():
            row = one("""UPDATE branch SET code = coalesce(%s, code), name_en = coalesce(%s, name_en),
                                address = CASE WHEN %s THEN %s ELSE address END, phone = CASE WHEN %s THEN %s ELSE phone END,
                                head_employee_id = CASE WHEN %s THEN %s::uuid ELSE head_employee_id END
                          WHERE id = %s AND company_id = %s RETURNING id, code, name_en""",
                      [b.get('code'), b.get('nameEn'), 'address' in b, b.get('address'), 'phone' in b, b.get('phone'),
                       'headEmployeeId' in b, b.get('headEmployeeId'), id_, company_id])
            if not row:
                raise NotFoundError('branch not found')
            audit('update', 'branch', actor_id=user.id, row_id=id_, after=b)
    except IntegrityError as e:
        if _unique_violation(e):
            raise _dup('branch') from e
        raise
    return _sid(row, 'id')


def delete_branch(user: AuthUser, id_: str) -> None:
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    if one('SELECT 1 AS x FROM employee_assignment WHERE branch_id = %s LIMIT 1', [id_]):
        raise ConflictError('people are (or were) assigned to this branch; move them before deleting it')
    try:
        with transaction.atomic():
            n = execute('DELETE FROM branch WHERE id = %s AND company_id = %s', [id_, company_id])
    except IntegrityError as e:
        if _fk_violation(e):
            raise ConflictError('this branch is still used by other records and cannot be deleted') from e
        raise
    if not n:
        raise NotFoundError('branch not found')
    audit('delete', 'branch', actor_id=user.id, row_id=id_)


UNIT_BODY = Obj({
    'kind': Enum(('department', 'team')), 'code': Str(trim=True, min=1, max=20), 'nameEn': Str(trim=True, min=1, max=120),
    'parentId': Uuid(nullable=True, optional=True), 'branchId': Uuid(nullable=True, optional=True),
    'headEmployeeId': Uuid(nullable=True, optional=True), 'description': Str(trim=True, max=500, nullable=True, optional=True),
}, strict=True)


def _assert_no_cycle(company_id: str, self_id: str, parent_id) -> None:
    """A unit may not sit under itself or under one of its own sub-units."""
    cur = parent_id
    guard = 0
    while cur and guard < 100:
        guard += 1
        if str(cur) == str(self_id):
            raise BadRequestError('a unit cannot sit under itself or one of its own sub-units')
        r = one('SELECT parent_id FROM org_unit WHERE id = %s AND company_id = %s', [cur, company_id])
        cur = r['parent_id'] if r else None


def create_unit(user: AuthUser, body: dict) -> dict:
    _hr(user)
    company_id = company_id_of(user)
    b = parse(UNIT_BODY, body)
    _owned_or_null(company_id, 'org_unit', b.get('parentId'), 'parent unit')
    _owned_or_null(company_id, 'branch', b.get('branchId'), 'branch')
    _owned_or_null(company_id, 'employee', b.get('headEmployeeId'), 'unit head')
    try:
        with transaction.atomic():
            row = _sid(one("""INSERT INTO org_unit (company_id, kind, code, name_en, parent_id, branch_id, head_employee_id, description)
                              VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, code, name_en, kind""",
                           [company_id, b['kind'], b['code'], b['nameEn'], b.get('parentId'), b.get('branchId'), b.get('headEmployeeId'),
                            b.get('description')]), 'id')
            audit('create', 'org_unit', actor_id=user.id, row_id=row['id'], after=b)
    except IntegrityError as e:
        if _unique_violation(e):
            raise _dup('department or team') from e
        raise
    return row


def update_unit(user: AuthUser, id_: str, body: dict) -> dict:
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    b = parse(_partial(UNIT_BODY), body)
    _owned_or_null(company_id, 'org_unit', b.get('parentId'), 'parent unit')
    _owned_or_null(company_id, 'branch', b.get('branchId'), 'branch')
    _owned_or_null(company_id, 'employee', b.get('headEmployeeId'), 'unit head')
    if 'parentId' in b:
        _assert_no_cycle(company_id, id_, b['parentId'])
    try:
        with transaction.atomic():
            row = one("""UPDATE org_unit SET kind = coalesce(%s, kind), code = coalesce(%s, code), name_en = coalesce(%s, name_en),
                                parent_id = CASE WHEN %s THEN %s::uuid ELSE parent_id END, branch_id = CASE WHEN %s THEN %s::uuid ELSE branch_id END,
                                head_employee_id = CASE WHEN %s THEN %s::uuid ELSE head_employee_id END,
                                description = CASE WHEN %s THEN %s ELSE description END
                          WHERE id = %s AND company_id = %s RETURNING id, code, name_en, kind""",
                      [b.get('kind'), b.get('code'), b.get('nameEn'), 'parentId' in b, b.get('parentId'), 'branchId' in b, b.get('branchId'),
                       'headEmployeeId' in b, b.get('headEmployeeId'), 'description' in b, b.get('description'), id_, company_id])
            if not row:
                raise NotFoundError('unit not found')
            audit('update', 'org_unit', actor_id=user.id, row_id=id_, after=b)
    except IntegrityError as e:
        if _unique_violation(e):
            raise _dup('department or team') from e
        raise
    return _sid(row, 'id')


def delete_unit(user: AuthUser, id_: str) -> None:
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    if one('SELECT 1 AS x FROM org_unit WHERE parent_id = %s LIMIT 1', [id_]):
        raise ConflictError('this unit has sub-units; move or delete them first')
    if one('SELECT 1 AS x FROM employee_assignment WHERE org_unit_id = %s LIMIT 1', [id_]):
        raise ConflictError('people are (or were) assigned to this unit; move them before deleting it')
    try:
        with transaction.atomic():
            execute('UPDATE designation SET org_unit_id = NULL WHERE org_unit_id = %s AND company_id = %s', [id_, company_id])
            n = execute('DELETE FROM org_unit WHERE id = %s AND company_id = %s', [id_, company_id])
            if n:
                audit('delete', 'org_unit', actor_id=user.id, row_id=id_)
    except IntegrityError as e:
        if _fk_violation(e):
            raise ConflictError('this unit is still used by other records and cannot be deleted') from e
        raise
    if not n:
        raise NotFoundError('unit not found')


DESIGNATION_BODY = Obj({
    'code': Str(trim=True, min=1, max=20), 'nameEn': Str(trim=True, min=1, max=120),
    'grade': Str(trim=True, max=40, nullable=True, optional=True), 'jobLevel': Int(min=1, max=20, nullable=True, optional=True),
    'orgUnitId': Uuid(nullable=True, optional=True), 'description': Str(trim=True, max=500, nullable=True, optional=True),
    'active': Bool(optional=True),
}, strict=True)


def create_designation(user: AuthUser, body: dict) -> dict:
    _hr(user)
    company_id = company_id_of(user)
    b = parse(DESIGNATION_BODY, body)
    _owned_or_null(company_id, 'org_unit', b.get('orgUnitId'), 'department')
    try:
        with transaction.atomic():
            row = _sid(one("""INSERT INTO designation (company_id, code, name_en, grade, job_level, org_unit_id, description, active)
                              VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, code, name_en, grade, job_level""",
                           [company_id, b['code'], b['nameEn'], b.get('grade'), b.get('jobLevel'), b.get('orgUnitId'), b.get('description'),
                            b.get('active', True)]), 'id')
            audit('create', 'designation', actor_id=user.id, row_id=row['id'], after=b)
    except IntegrityError as e:
        if _unique_violation(e):
            raise _dup('designation') from e
        raise
    return row


def update_designation(user: AuthUser, id_: str, body: dict) -> dict:
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    b = parse(_partial(DESIGNATION_BODY), body)
    _owned_or_null(company_id, 'org_unit', b.get('orgUnitId'), 'department')
    with transaction.atomic():
        try:
            with transaction.atomic():
                row = one("""UPDATE designation SET code = coalesce(%s, code), name_en = coalesce(%s, name_en), grade = CASE WHEN %s THEN %s ELSE grade END,
                                    job_level = CASE WHEN %s THEN %s::smallint ELSE job_level END,
                                    org_unit_id = CASE WHEN %s THEN %s::uuid ELSE org_unit_id END,
                                    description = CASE WHEN %s THEN %s ELSE description END, active = coalesce(%s, active)
                              WHERE id = %s AND company_id = %s RETURNING id, code, name_en, grade, job_level""",
                          [b.get('code'), b.get('nameEn'), 'grade' in b, b.get('grade'), 'jobLevel' in b, b.get('jobLevel'),
                           'orgUnitId' in b, b.get('orgUnitId'), 'description' in b, b.get('description'), b.get('active'), id_, company_id])
        except IntegrityError as e:
            if _unique_violation(e):
                raise _dup('designation') from e
            raise
        if not row:
            raise NotFoundError('designation not found')
        # Renaming a designation renames it on everyone who holds it.
        if b.get('nameEn'):
            execute('UPDATE employee SET designation = %s, updated_at = now() WHERE designation_id = %s AND designation IS DISTINCT FROM %s',
                    [b['nameEn'], id_, b['nameEn']])
        audit('update', 'designation', actor_id=user.id, row_id=id_, after=b)
    return _sid(row, 'id')


def delete_designation(user: AuthUser, id_: str) -> None:
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    if one('SELECT 1 AS x FROM employee WHERE designation_id = %s LIMIT 1', [id_]):
        raise ConflictError('people hold this designation; mark it inactive instead')
    try:
        with transaction.atomic():
            n = execute('DELETE FROM designation WHERE id = %s AND company_id = %s', [id_, company_id])
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
    p = parse(POLICY_SCHEMA, body)
    before = people_policy(company_id)
    with transaction.atomic():
        execute("""INSERT INTO company_setting (company_id, key, value, updated_by) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
                [company_id, PEOPLE_POLICY, json.dumps(p), user.id])
        audit('update', 'company_setting', actor_id=user.id, row_id=PEOPLE_POLICY, before=before, after=p)
    return p


# ---------------------------------------------------------------- my change requests and photo

CHANGE_BODY = Obj({
    'section': Str(max=40), 'value': Any_(optional=True), 'reason': Str(trim=True, max=500, optional=True),
    'proofFileId': Uuid(optional=True),
}, strict=True)


def _with_label(r: dict) -> dict:
    return {**r, 'sectionLabel': SECTION_LABEL.get(r['section'], r['section'])}


def request_change(user: AuthUser, body: dict) -> dict:
    """POST /people/me/change-requests."""
    from hajiri.files.store import attach_file
    company_id = company_id_of(user)
    employee_id = user.employee_id
    if not employee_id:
        raise ForbiddenError('only employees have a profile to change')
    b = parse(CHANGE_BODY, body)
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
    return [_with_label(req_row(r)) for r in rows(
        f"SELECT {', '.join(REQ_COLS)} FROM profile_change_request WHERE employee_id = %s AND company_id = %s ORDER BY created_at DESC LIMIT 100",
        [user.employee_id, company_id_of(user)])]


def cancel_my_request(user: AuthUser, id_: str) -> None:
    from hajiri.workflow.services import cancel_workflow
    id_ = parse_uuid(str(id_))
    company_id = company_id_of(user)
    with transaction.atomic():
        x = one('SELECT * FROM profile_change_request WHERE id = %s AND company_id = %s FOR UPDATE', [id_, company_id])
        if not x or (str(x['employee_id']) != user.employee_id and str(x['requested_by']) != user.id):
            raise NotFoundError('change request not found')
        if x['status'] != 'pending':
            raise ConflictError(f"this request is already {x['status']}")
        execute("UPDATE profile_change_request SET status = 'cancelled', decided_at = now(), decided_by = %s WHERE id = %s", [user.id, id_])
        cancel_workflow(PROFILE_CHANGE, id_)


def set_avatar(user: AuthUser, employee_id: str, file_id: str | None) -> None:
    from hajiri.files.store import attach_file, delete_file
    company_id = company_id_of(user)
    with transaction.atomic():
        emp = one('SELECT avatar_file_id FROM employee WHERE id = %s AND company_id = %s FOR UPDATE', [employee_id, company_id])
        if not emp:
            raise NotFoundError('employee not found')
        if file_id:
            attach_file(company_id=company_id, file_id=file_id, user_id=user.id, purpose='avatar', attached_to=f'employee_avatar:{employee_id}')
        old = emp['avatar_file_id'] and str(emp['avatar_file_id'])
        execute('UPDATE employee SET avatar_file_id = %s, updated_at = now() WHERE id = %s', [file_id, employee_id])
        if old and old != file_id:
            delete_file(company_id, old)
        audit('update' if file_id else 'delete', 'employee_avatar', actor_id=user.id, row_id=employee_id)


AVATAR_BODY = Obj({'fileId': Uuid()}, strict=True)


def set_my_avatar(user: AuthUser, body: dict | None) -> None:
    if not user.employee_id:
        raise ForbiddenError('only employees have a profile photo')
    set_avatar(user, user.employee_id, None if body is None else parse(AVATAR_BODY, body)['fileId'])


def set_employee_avatar(user: AuthUser, employee_id: str, body: dict | None) -> None:
    _hr(user)
    employee_id = parse_uuid(str(employee_id))
    set_avatar(user, employee_id, None if body is None else parse(AVATAR_BODY, body)['fileId'])


# ---------------------------------------------------------------- change requests (HR)

REQUEST_STATUSES = ('pending', 'approved', 'rejected', 'cancelled', 'all')


def change_requests(user: AuthUser, status: str = 'pending') -> list[dict]:
    _hr(user)
    status = parse(Enum(REQUEST_STATUSES), status or 'pending')
    return [_with_label(req_row(r)) for r in rows(
        f"""SELECT {', '.join('p.' + c for c in REQ_COLS)}, e.name_en AS employee_name, e.emp_code, u.name AS requested_by_name,
                   w.id AS instance_id
              FROM profile_change_request p JOIN employee e ON e.id = p.employee_id LEFT JOIN app_user u ON u.id = p.requested_by
              LEFT JOIN workflow_instance w ON w.object_type = '{PROFILE_CHANGE}' AND w.object_id = p.id
             WHERE p.company_id = %s AND (%s = 'all' OR p.status = %s) ORDER BY p.created_at DESC LIMIT 300""",
        [company_id_of(user), status, status])]


def load_request(user: AuthUser, id_: str) -> dict:
    id_ = parse_uuid(str(id_))
    row = one('SELECT * FROM profile_change_request WHERE id = %s AND company_id = %s', [id_, company_id_of(user)])
    if not row:
        raise NotFoundError('change request not found')
    if not has_role(user, 'hr') and str(row['employee_id']) != user.employee_id:
        raise NotFoundError('change request not found')
    return row


def request_detail(user: AuthUser, id_: str, *, reveal: bool = False) -> dict:
    row = load_request(user, id_)
    reveal = reveal and has_role(user, 'hr')
    if row['status'] == 'pending':
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
    p = one(f'SELECT {DIRECTORY_COLS}, e.skills, e.languages, e.status FROM {DIRECTORY_FROM} WHERE e.id = %s AND e.company_id = %s',
            [id_, company_id])
    if not p:
        raise NotFoundError('person not found')
    reports = [_sid(r, 'id') for r in rows(
        """SELECT e.id, e.name_en, e.designation, e.avatar_file_id IS NOT NULL AS has_avatar FROM employee_assignment a
             JOIN employee e ON e.id = a.employee_id
            WHERE a.manager_id = %s AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date AND e.status = 'active' ORDER BY e.name_en""", [id_])]
    p = _sid(p, *_DIR_IDS)
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
    pending = [_sid(r, 'id') for r in rows("SELECT id, section, created_at FROM profile_change_request WHERE employee_id = %s AND status = 'pending'", [id_])]
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
        one('SELECT 1 AS x FROM employee WHERE id = %s FOR UPDATE', [id_])
        ch = apply_section(company_id=company_id, employee_id=id_, section=section, value=value, actor_id=user.id, source='hr_edit')
        if ch:
            audit('update', 'employee_profile', actor_id=user.id, row_id=id_, after={'section': section, 'fields': [x['label'] for x in ch]})
    return {'pending': False, 'changes': ch}


def history(user: AuthUser, id_: str) -> list[dict]:
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    employee_in(company_id, id_)
    self_or_hr(user, id_)
    return rows("""SELECT h.id, h.field, h.old_value, h.new_value, h.source, h.ref_id, h.changed_at, u.name AS changed_by_name
                     FROM employee_field_history h LEFT JOIN app_user u ON u.id = h.changed_by
                    WHERE h.employee_id = %s AND h.company_id = %s ORDER BY h.changed_at DESC, h.id DESC LIMIT 500""", [id_, company_id])


def career(user: AuthUser, id_: str) -> list[dict]:
    """GET /people/:id/career: HR and the employee see everything; their manager sees the events without reasons,
    separation types, salary revisions or suspensions."""
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    employee_in(company_id, id_)
    full = has_role(user, 'hr') or user.employee_id == id_
    if not full and not (has_role(user, 'manager') and user.employee_id and is_manager_of(user.employee_id, id_)):
        raise ForbiddenError('not allowed to see this career history')
    evs = rows("""SELECT c.*, u.name AS created_by_name FROM career_event c LEFT JOIN app_user u ON u.id = c.created_by
                   WHERE c.employee_id = %s AND c.company_id = %s AND c.status <> 'cancelled' ORDER BY c.effective_date DESC, c.created_at DESC""",
               [id_, company_id])
    names: dict[str, str] = {}

    def lookup(table: str, key) -> str | None:
        if not key:
            return None
        if key not in names:
            r = one(f'SELECT name_en AS n FROM {table} WHERE id = %s', [key])
            names[key] = r['n'] if r else '—'
        return names[key]
    out = []
    for ev in evs:
        d = ev['details'] or {}
        parts: list[str] = []
        designation = d.get('designation') or lookup('designation', d.get('designationId'))
        if designation:
            parts.append(designation)
        if d.get('grade'):
            parts.append(f"grade {d['grade']}")
        branch = lookup('branch', d.get('branchId'))
        if branch:
            parts.append(f'{branch} branch')
        unit = lookup('org_unit', d.get('orgUnitId'))
        if unit:
            parts.append(unit)
        if 'managerId' in d:
            parts.append(f"reports to {lookup('employee', d['managerId']) or 'nobody'}")
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
    return out if full else [e for e in out if e['kind'] not in ('salary_revision', 'suspension')]


def record_career(user: AuthUser, id_: str, body: dict) -> dict:
    _hr(user)
    company_id = company_id_of(user)
    id_ = parse_uuid(str(id_))
    b = parse(career_mod.CAREER_BODY, body)
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

