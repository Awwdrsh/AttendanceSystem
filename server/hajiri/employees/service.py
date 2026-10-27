"""
The employee master (port of apps/api/src/modules/employees/routes.ts): list, read, sensitive fields, create, edit with
effective-dated assignments and field history, CSV import, assignment history and the employee-visible audit trail.
"""
from __future__ import annotations

from datetime import date

from django.db import transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, assert_manages, company_id_of, has_role
from hajiri.core.clock import npt_today
from hajiri.core.crypto import field_cipher
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import MISSING, Arr, Bool, Enum, Int, IsoDate, Obj, Str, Uuid, parse, parse_uuid, safe_parse

PAN_RE = r'^\d{9}$'
EMPLOYMENT_TYPES = ('regular', 'work_based', 'time_based', 'casual', 'part_time')

EMPLOYEE_FIELDS = {
    'empCode': Str(min=1, max=32),
    'nameEn': Str(min=1, max=120),
    'nameNe': Str(max=120, optional=True),
    'gender': Enum(('male', 'female', 'other'), optional=True),
    'dob': IsoDate(optional=True),
    'maritalStatus': Enum(('single', 'couple'), optional=True),
    'joinDate': IsoDate(optional=True),
    'exitDate': IsoDate(nullable=True, optional=True),
    'designation': Str(max=120, optional=True),
    'grade': Str(max=40, optional=True),
    'phone': Str(max=30, optional=True),
    'email': Str(email=True, optional=True),
    'otEligible': Bool(optional=True),
    'workFromHome': Bool(optional=True),
    'weeklyOffDays': Arr(Int(min=0, max=6), optional=True),
    'regime': Enum(('ssf', 'pf', 'none'), optional=True),
    'ssfId': Str(max=20, optional=True),
    'pfId': Str(max=40, optional=True),
    'citId': Str(max=40, optional=True),
    'pan': Str(regex=PAN_RE, regex_message='PAN must be 9 digits', optional=True),
    'bankCode': Str(max=20, optional=True),
    'bankBranch': Str(max=80, optional=True),
    'bankAccount': Str(min=4, max=40, optional=True),
    'isResident': Bool(optional=True),
    'remoteAreaClass': Enum(('A', 'B', 'C', 'D'), nullable=True, optional=True),
    'disability': Bool(optional=True),
    'festivalMonth': Int(min=1, max=12, nullable=True, optional=True),
    'probationEnd': IsoDate(nullable=True, optional=True),
    'branchId': Uuid(optional=True),
    'orgUnitId': Uuid(nullable=True, optional=True),
    'managerId': Uuid(nullable=True, optional=True),
    'employmentType': Enum(EMPLOYMENT_TYPES, optional=True),
    'shiftId': Uuid(optional=True),
}
EMPLOYEE_BODY = Obj(EMPLOYEE_FIELDS)


def _partial_body() -> Obj:
    from copy import copy
    out = {}
    for k, f in EMPLOYEE_FIELDS.items():
        f2 = copy(f)
        f2.optional = True
        out[k] = f2
    return Obj(out)


EMPLOYEE_PATCH = _partial_body()

PUBLIC_COLS = """e.id, e.company_id, e.emp_code, e.name_en, e.name_ne, e.gender, e.dob, e.marital_status, e.join_date, e.exit_date, e.designation,
  e.grade, e.phone, e.email, e.ot_eligible, e.work_from_home, e.weekly_off_days, e.status, e.regime, e.ssf_id, e.pf_id, e.cit_id, e.bank_code,
  e.bank_branch, e.bank_account_last4, e.is_resident, e.remote_area_class, e.disability, e.festival_month, e.probation_end, e.created_at,
  (e.pan_enc IS NOT NULL) AS has_pan,
  a.branch_id, a.org_unit_id, a.manager_id, a.employment_type,
  (SELECT s.shift_id FROM shift_assignment s WHERE s.employee_id = e.id AND s.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date) AS shift_id"""
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


def like_escape(s: str) -> str:
    """Search text is matched literally: % and _ typed by the user are not wildcards."""
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _d(s) -> date | None:
    return date.fromisoformat(s) if isinstance(s, str) else s


def validate_refs(company_id: str, b: dict, self_id: str | None = None) -> None:
    """Referenced records (shift, branch, org unit, manager) must belong to the caller's company; dates must be sane."""
    def check(table: str, id_, label: str) -> None:
        if not id_:
            return
        if not one(f'SELECT 1 AS x FROM {table} WHERE id = %s AND company_id = %s', [id_, company_id]):
            raise BadRequestError(f'{label} does not exist in your company')
    check('shift', b.get('shiftId'), 'shift')
    check('branch', b.get('branchId'), 'branch')
    check('org_unit', b.get('orgUnitId'), 'org unit')
    check('employee', b.get('managerId'), 'manager')
    if b.get('managerId') and self_id and b['managerId'] == self_id:
        raise BadRequestError('an employee cannot be their own manager')
    if b.get('joinDate') and b.get('exitDate') and b['exitDate'] < b['joinDate']:
        raise BadRequestError('exit date cannot be before join date')
    if b.get('probationEnd') and b.get('joinDate') and b['probationEnd'] < b['joinDate']:
        raise BadRequestError('probation end cannot be before join date')


def link_designation(employee_id: str) -> None:
    """Keep designation_id in step with the free-text designation: link it to the catalogue entry of the same name, if any."""
    execute("""UPDATE employee e SET designation_id = (SELECT d.id FROM designation d WHERE d.company_id = e.company_id
                                                       AND lower(d.name_en) = lower(e.designation) ORDER BY d.active DESC LIMIT 1)
                WHERE e.id = %s""", [employee_id])


def pan_owner(company_id: str, pan: str, except_id: str | None = None) -> str | None:
    """emp_code of the employee already holding this PAN in the company (other than except_id), via the blind index."""
    r = one('SELECT emp_code FROM employee WHERE company_id = %s AND pan_bidx = %s AND (%s::uuid IS NULL OR id <> %s) LIMIT 1',
            [company_id, field_cipher().blind_index(pan), except_id, except_id])
    return r['emp_code'] if r else None


def assert_pan_free(company_id: str, pan: str, except_id: str | None = None) -> None:
    owner = pan_owner(company_id, pan, except_id)
    if owner:
        raise ConflictError(f'this PAN is already recorded for employee {owner}')


LIST_QUERY = Obj({'status': Enum(('active', 'exited'), optional=True), 'search': Str(max=100, optional=True),
                  'branchId': Uuid(optional=True), 'pan': Str(regex=PAN_RE, optional=True)})


def list_employees(user: AuthUser, query: dict | None = None) -> list[dict]:
    """HR (and the payroll and attendance admins, read-only) list the whole company; a manager lists the people they
    manage and themselves."""
    if not has_role(user, 'hr', 'manager', *READ_ALL):
        raise ForbiddenError(f"requires role {' or '.join(('hr', 'manager', *READ_ALL))}")
    q = parse(LIST_QUERY, {k: v for k, v in (query or {}).items() if v not in (None, '')})
    if q.get('pan') and not has_role(user, 'hr'):
        raise ForbiddenError('PAN lookup requires HR')
    search = like_escape(q['search']) if q.get('search') else None
    out = [_row(r) for r in rows(
        f"""SELECT {PUBLIC_COLS} FROM employee e
              LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
             WHERE e.company_id = %s AND (%s::text IS NULL OR e.status = %s)
               AND (%s::text IS NULL OR e.emp_code ILIKE '%%' || %s || '%%' OR e.name_en ILIKE '%%' || %s || '%%' OR e.name_ne ILIKE '%%' || %s || '%%')
               AND (%s::uuid IS NULL OR a.branch_id = %s) AND (%s::text IS NULL OR e.pan_bidx = %s)
               AND (%s OR e.id = %s::uuid OR hj_manages(%s::uuid, e.id, (now() AT TIME ZONE 'Asia/Kathmandu')::date))
             ORDER BY e.emp_code""",
        [user.company_id, q.get('status'), q.get('status'), search, search, search, search, q.get('branchId'), q.get('branchId'),
         *([field_cipher().blind_index(q['pan'])] * 2 if q.get('pan') else [None, None]),
         has_role(user, 'hr', *READ_ALL), user.employee_id, user.employee_id])]
    if q.get('pan'):
        audit('pan_lookup', 'employee', actor_id=user.id, after={'matches': len(out)})
    return out


def _public(id_: str, company_id: str | None = None) -> dict | None:
    if company_id is None:
        return _row(one(f"""SELECT {PUBLIC_COLS} FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
                            WHERE e.id = %s""", [id_]))
    return _row(one(f"""SELECT {PUBLIC_COLS} FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
                        WHERE e.id = %s AND e.company_id = %s""", [id_, company_id]))


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
    row = one('SELECT pan_enc, bank_account_enc FROM employee WHERE id = %s AND company_id = %s', [id_, user.company_id])
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


def create_employee(user: AuthUser, body: dict) -> str:
    from hajiri.lifecycle.service import auto_start_onboarding
    from hajiri.people.career import record_career_event
    _require_hr(user)
    company_id = company_id_of(user)
    b = parse(EMPLOYEE_BODY, body)
    validate_refs(company_id, b)
    if b.get('pan'):
        assert_pan_free(company_id, b['pan'])
    c = field_cipher()
    with transaction.atomic():
        id_ = str(one("""INSERT INTO employee (company_id, emp_code, name_en, name_ne, gender, dob, marital_status, join_date, designation, grade,
                                               phone, email, ot_eligible, weekly_off_days, regime, ssf_id, pf_id, cit_id, bank_code, bank_branch,
                                               is_resident, remote_area_class, disability, festival_month, probation_end, work_from_home)
                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                      [company_id, b['empCode'], b['nameEn'], b.get('nameNe'), b.get('gender'), b.get('dob'), b.get('maritalStatus'),
                       b.get('joinDate'), b.get('designation'), b.get('grade'), b.get('phone'), b.get('email'), b.get('otEligible', True),
                       b.get('weeklyOffDays', [6]), b.get('regime', 'ssf'), b.get('ssfId'), b.get('pfId'), b.get('citId'), b.get('bankCode'),
                       b.get('bankBranch'), b.get('isResident', True), b.get('remoteAreaClass'), b.get('disability', False),
                       b.get('festivalMonth'), b.get('probationEnd'), b.get('workFromHome', False)])['id'])
        if b.get('pan'):
            execute('UPDATE employee SET pan_enc = %s, pan_bidx = %s WHERE id = %s',
                    [c.encrypt(b['pan'], f'employee:{id_}:pan'), c.blind_index(b['pan']), id_])
        if b.get('bankAccount'):
            execute('UPDATE employee SET bank_account_enc = %s, bank_account_last4 = %s WHERE id = %s',
                    [c.encrypt(b['bankAccount'], f'employee:{id_}:bank'), b['bankAccount'][-4:], id_])
        frm = b.get('joinDate') or npt_today().isoformat()
        execute("""INSERT INTO employee_assignment (employee_id, valid, branch_id, org_unit_id, manager_id, employment_type)
                   VALUES (%s, daterange(%s, NULL), %s, %s, %s, %s)""",
                [id_, frm, b.get('branchId'), b.get('orgUnitId'), b.get('managerId'), b.get('employmentType', 'regular')])
        if b.get('shiftId'):
            execute('INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES (%s, %s, daterange(%s, NULL))', [id_, b['shiftId'], frm])
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


def update_employee(user: AuthUser, id_: str, body: dict) -> dict:
    from hajiri.people.history import diff_employee_rows, record_field_changes
    from hajiri.people.service import people_policy
    _require_hr(user)
    id_ = parse_uuid(str(id_))
    company_id = company_id_of(user)
    b = parse(EMPLOYEE_PATCH, body)
    validate_refs(company_id, b, id_)
    before = _public(id_, company_id)
    if not before:
        raise NotFoundError('employee not found')
    if (b.get('pan') or b.get('bankAccount')) and people_policy(company_id)['makerChecker']:
        raise ConflictError("PAN and bank account changes need a second HR approval: edit them under the employee's profile (ID documents, Bank account)")
    stored_join = before['join_date']
    if b.get('exitDate') and stored_join and not b.get('joinDate') and _d(b['exitDate']) < stored_join:
        raise BadRequestError('exit date cannot be before join date')
    c = field_cipher()
    with transaction.atomic():
        sets: list[str] = []
        vals: list = []
        for k, col in _PATCH_MAP:
            if k in b:
                sets.append(f'{col} = %s')
                vals.append(b[k])
        if b.get('exitDate'):
            sets.append("status = 'exited'")
        elif 'exitDate' in b and b['exitDate'] is None:
            sets.append("status = 'active'")
        if b.get('pan'):
            assert_pan_free(company_id, b['pan'], id_)
            sets += ['pan_enc = %s', 'pan_bidx = %s']
            vals += [c.encrypt(b['pan'], f'employee:{id_}:pan'), c.blind_index(b['pan'])]
        if b.get('bankAccount'):
            sets += ['bank_account_enc = %s', 'bank_account_last4 = %s']
            vals += [c.encrypt(b['bankAccount'], f'employee:{id_}:bank'), b['bankAccount'][-4:]]
        if sets:
            execute(f"UPDATE employee SET {', '.join(sets)}, updated_at = now() WHERE id = %s", vals + [id_])
        if any(k in b for k in ('branchId', 'orgUnitId', 'managerId', 'employmentType')):
            # Effective-dated: close the current assignment today and open a new one (plan §9.1 decision 2).
            today = npt_today()
            cur = one("""SELECT lower(valid) AS lo, branch_id, org_unit_id, manager_id, employment_type FROM employee_assignment
                          WHERE employee_id = %s AND valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date""", [id_])
            nxt = {
                'branch_id': b.get('branchId') or (cur and cur['branch_id']) or None,
                'org_unit_id': b['orgUnitId'] if 'orgUnitId' in b else (cur and cur['org_unit_id']) or None,
                'manager_id': b['managerId'] if 'managerId' in b else (cur and cur['manager_id']) or None,
                'employment_type': b.get('employmentType') or (cur and cur['employment_type']) or 'regular',
            }
            if cur:
                if cur['lo'] == today:
                    execute('DELETE FROM employee_assignment WHERE employee_id = %s AND valid @> (now() AT TIME ZONE \'Asia/Kathmandu\')::date', [id_])
                else:
                    execute('UPDATE employee_assignment SET valid = daterange(lower(valid), %s::date) WHERE employee_id = %s AND valid @> (now() AT TIME ZONE \'Asia/Kathmandu\')::date',
                            [today, id_])
            execute("""INSERT INTO employee_assignment (employee_id, valid, branch_id, org_unit_id, manager_id, employment_type)
                       VALUES (%s, daterange(%s, NULL), %s, %s, %s, %s)""",
                    [id_, today, nxt['branch_id'], nxt['org_unit_id'], nxt['manager_id'], nxt['employment_type']])
        if b.get('shiftId'):
            today = npt_today()
            execute("""UPDATE shift_assignment SET valid = daterange(lower(valid), %s::date)
                        WHERE employee_id = %s AND valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date AND lower(valid) < %s::date""", [today, id_, today])
            execute('DELETE FROM shift_assignment WHERE employee_id = %s AND lower(valid) = %s::date', [id_, today])
            execute('INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES (%s, %s, daterange(%s, NULL))', [id_, b['shiftId'], today])
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
    """Minimal RFC 4180 CSV parser (quotes, escaped quotes, CRLF), as the TS one."""
    out: list[list[str]] = []
    row: list[str] = []
    field = ''
    in_q = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_q:
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':
                    field += '"'
                    i += 1
                else:
                    in_q = False
            else:
                field += ch
        elif ch == '"':
            in_q = True
        elif ch == ',':
            row.append(field)
            field = ''
        elif ch in '\n\r':
            if ch == '\r' and i + 1 < n and text[i + 1] == '\n':
                i += 1
            row.append(field)
            out.append(row)
            row, field = [], ''
        else:
            field += ch
        i += 1
    if field or row:
        row.append(field)
        out.append(row)
    return [r for r in out if any(f.strip() != '' for f in r)]


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
    for i in range(1, len(data)):
        rec = {h: (data[i][j].strip() if j < len(data[i]) else '') for j, h in enumerate(header)}
        cand = {
            'empCode': rec.get('emp_code', ''), 'nameEn': rec.get('name_en', ''), 'nameNe': rec.get('name_ne') or MISSING,
            'gender': rec.get('gender') or MISSING, 'dob': rec.get('dob') or MISSING, 'maritalStatus': rec.get('marital_status') or MISSING,
            'joinDate': rec.get('join_date') or MISSING, 'designation': rec.get('designation') or MISSING, 'grade': rec.get('grade') or MISSING,
            'phone': rec.get('phone') or MISSING, 'email': rec.get('email') or MISSING, 'regime': rec.get('regime') or MISSING,
            'ssfId': rec.get('ssf_id') or MISSING, 'pan': rec.get('pan') or MISSING, 'bankCode': rec.get('bank_code') or MISSING,
            'bankBranch': rec.get('bank_branch') or MISSING, 'bankAccount': rec.get('bank_account') or MISSING,
            'otEligible': (rec['ot_eligible'].lower() == 'true' or rec['ot_eligible'] == '1') if rec.get('ot_eligible') else MISSING,
            'workFromHome': rec['work_from_home'].lower() in ('true', '1', 'yes') if rec.get('work_from_home') else MISSING,
        }
        if 'emp_code' not in rec:
            cand['empCode'] = MISSING
        ok, res = safe_parse(EMPLOYEE_BODY, {k: v for k, v in cand.items() if v is not MISSING})
        if not ok:
            for iss in res.issues:
                errors.append({'row': i + 1, 'message': f"{iss['path']}: {iss['message']}"})
            continue
        valid.append(res)
    codes = [v['empCode'] for v in valid]
    seen_dup: list[str] = []
    for j, cd in enumerate(codes):
        if codes.index(cd) != j and cd not in seen_dup:
            seen_dup.append(cd)
    for d in seen_dup:
        errors.append({'row': 0, 'message': f'duplicate emp_code in file: {d}'})
    pans = [v['pan'] for v in valid if v.get('pan')]
    dup_pans: list[str] = []
    for j, p in enumerate(pans):
        if pans.index(p) != j and p not in dup_pans:
            dup_pans.append(p)
    for d in dup_pans:
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
    c = field_cipher()
    imported = 0
    with transaction.atomic():
        for b in valid:
            ins = one("""INSERT INTO employee (company_id, emp_code, name_en, name_ne, gender, dob, marital_status, join_date, designation, grade, phone,
                                               email, ot_eligible, regime, ssf_id, bank_code, bank_branch, work_from_home)
                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                         ON CONFLICT (company_id, emp_code) DO NOTHING RETURNING id""",
                      [company_id, b['empCode'], b['nameEn'], b.get('nameNe'), b.get('gender'), b.get('dob'), b.get('maritalStatus'),
                       b.get('joinDate'), b.get('designation'), b.get('grade'), b.get('phone'), b.get('email'), b.get('otEligible', True),
                       b.get('regime', 'ssf'), b.get('ssfId'), b.get('bankCode'), b.get('bankBranch'), b.get('workFromHome', False)])
            if not ins:
                errors.append({'row': 0, 'message': f"emp_code {b['empCode']} already exists; skipped"})
                continue
            id_ = str(ins['id'])
            if b.get('pan'):
                execute('UPDATE employee SET pan_enc = %s, pan_bidx = %s WHERE id = %s',
                        [c.encrypt(b['pan'], f'employee:{id_}:pan'), c.blind_index(b['pan']), id_])
            if b.get('bankAccount'):
                execute('UPDATE employee SET bank_account_enc = %s, bank_account_last4 = %s WHERE id = %s',
                        [c.encrypt(b['bankAccount'], f'employee:{id_}:bank'), b['bankAccount'][-4:], id_])
            frm = b.get('joinDate') or npt_today().isoformat()
            execute('INSERT INTO employee_assignment (employee_id, valid, employment_type) VALUES (%s, daterange(%s, NULL), %s)', [id_, frm, 'regular'])
            if b.get('designation'):
                link_designation(id_)
            details = {**({'designation': b['designation']} if b.get('designation') else {}), **({'grade': b['grade']} if b.get('grade') else {})}
            record_career_event(company_id=company_id, employee_id=id_, kind='hire', effective_date=frm, details=details, actor_id=user.id)
            imported += 1
        audit('import', 'employee', actor_id=user.id, after={'imported': imported, 'skipped': len(errors)})
    return 201, {'imported': imported, 'errors': errors}


def assignments(user: AuthUser, id_: str) -> list[dict]:
    """Effective-dated assignment history."""
    id_ = parse_uuid(str(id_))
    assert_manages(user, id_)
    return [{**r, 'employee_id': str(r['employee_id']), 'branch_id': r['branch_id'] and str(r['branch_id']),
             'org_unit_id': r['org_unit_id'] and str(r['org_unit_id']), 'manager_id': r['manager_id'] and str(r['manager_id'])}
            for r in rows("""SELECT a.employee_id, a.valid::text AS valid, lower(a.valid) AS valid_from, upper(a.valid) AS valid_to, a.branch_id,
                                    a.org_unit_id, a.manager_id, a.employment_type, a.probation_end, b.name_en AS branch_name, m.name_en AS manager_name
                               FROM employee_assignment a LEFT JOIN branch b ON b.id = a.branch_id LEFT JOIN employee m ON m.id = a.manager_id
                              WHERE a.employee_id = %s ORDER BY lower(a.valid)""", [id_])]


def audit_events(user: AuthUser, id_: str) -> list[dict]:
    """Employee-visible audit of their own record (plan §5.11)."""
    id_ = parse_uuid(str(id_))
    assert_manages(user, id_)
    return rows("""SELECT l.id, l.occurred_at, l.actor_id, l.action, l.table_name, l.row_id, u.name AS actor_name
                     FROM audit_log l LEFT JOIN app_user u ON u.id::text = l.actor_id::text WHERE l.row_id = %s ORDER BY l.id DESC LIMIT 200""",
                [id_])
