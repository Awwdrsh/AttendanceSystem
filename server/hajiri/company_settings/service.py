"""
Company settings, branches and org units (port of apps/api/src/modules/settings/routes.ts). Everything but the header
profile is for HR and admin; the functions check that themselves.
"""
from __future__ import annotations

import json

from django.db import transaction

from engines.attendance import DEFAULT_POLICY, AttendancePolicy, ComputeDayInput, DayContext, compute_day
from engines.bs_calendar import BsRangeError, ad_to_bs, format_bs, weekday_of
from dataclasses import replace

from hajiri.attendance import repo
from hajiri.attendance.service import date_range
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, has_role
from hajiri.core.clock import npt_today
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError
from hajiri.core.schema import Any_, Arr, Enum, IsoDate, Obj, Record, Str, Uuid, parse, safe_parse

from .schemas import OT_CAP_DAY_MIN, OT_CAP_WEEK_MIN, SETTING_DESCRIPTIONS, SETTING_KEYS, SETTING_SCHEMAS, policy_version

LOCKED = {'otCapDayMin': OT_CAP_DAY_MIN, 'otCapWeekMin': OT_CAP_WEEK_MIN,
          'note': 'Labour Act 2074 Sec 30(1): overtime caps cannot be raised above 4 h/day and 24 h/week.'}


def _hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr or admin')


def get_setting(company_id: str, key: str) -> dict | None:
    """A stored setting value (None when the company never saved it)."""
    r = one('SELECT value FROM company_setting WHERE company_id = %s AND key = %s', [company_id, key])
    return r['value'] if r else None


def company_header(user: AuthUser) -> dict:
    """Name, logo and address for the app header (any signed-in user), and today in AD and BS."""
    c = one('SELECT id, code, name_en, name_ne FROM company WHERE id = %s', [user.company_id])
    if not c:
        raise NotFoundError('company not found')
    profile = get_setting(user.company_id, 'company.profile') or {}
    today_ad = npt_today().isoformat()
    try:
        today_bs = format_bs(ad_to_bs(today_ad))
    except BsRangeError:
        today_bs = None
    pick = lambda k, d: profile.get(k) if profile.get(k) is not None else d  # noqa: E731 - TS `??`
    return {'company': {**c, 'nameEn': pick('nameEn', c['name_en']), 'nameNe': pick('nameNe', c['name_ne']),
                        'logoDataUrl': profile.get('logoDataUrl'), 'address': profile.get('address')},
            'today': {'ad': today_ad, 'bs': today_bs}}


def list_settings(user: AuthUser) -> dict:
    _hr(user)
    stored = {r['key']: r for r in rows('SELECT key, value, updated_at, updated_by FROM company_setting WHERE company_id = %s',
                                        [user.company_id])}
    # An unsaved company profile defaults to the registered name, so it can be edited and saved back as is.
    c = one('SELECT name_en FROM company WHERE id = %s', [user.company_id])
    company_name = c['name_en'] if c else 'Company'
    out = []
    for key in SETTING_KEYS:
        row = stored.get(key)
        if row:
            value = row['value']
        else:
            ok, parsed = safe_parse(SETTING_SCHEMAS[key], {'nameEn': company_name} if key == 'company.profile' else {})
            value = parsed if ok else None
        out.append({'key': key, 'description': SETTING_DESCRIPTIONS[key], 'value': value,
                    'updatedAt': row['updated_at'] if row else None, 'updatedBy': row['updated_by'] if row else None})
    return {'settings': out, 'locked': LOCKED}


@transaction.atomic
def put_setting(user: AuthUser, key: str, body) -> dict:
    _hr(user)
    if key not in SETTING_KEYS:
        raise NotFoundError(f'unknown setting {key}')
    parsed = parse(SETTING_SCHEMAS[key], body)
    if key == 'attendance.policy':
        parsed['version'] = policy_version(parsed)
    before = get_setting(user.company_id, key)
    execute("""INSERT INTO company_setting (company_id, key, value, updated_by) VALUES (%s, %s, %s, %s)
               ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
            [user.company_id, key, json.dumps(parsed), user.id])
    audit('update', 'company_setting', actor_id=user.id, row_id=key, before=before, after=parsed)
    return {'key': key, 'value': parsed}


PREVIEW = Obj({'policy': Record(Any_()), 'from': IsoDate(), 'to': IsoDate(), 'employeeIds': Arr(Uuid(), max=200, optional=True)})


def preview_attendance(user: AuthUser, body: dict) -> dict:
    """'Applying to last month': recompute in memory with a proposed attendance policy and compare late marks,
    half-days, absents and detected OT with what is stored. Writes nothing."""
    _hr(user)
    b = parse(PREVIEW, body)
    dates = date_range(b['from'], b['to'])
    if not dates or len(dates) > 31:
        raise BadRequestError('preview range must be 1 to 31 days')
    raw = parse(SETTING_SCHEMAS['attendance.policy'], b['policy'])
    proposed = AttendancePolicy(version='preview', holiday_work=raw['holidayWork'], ot=replace(
        DEFAULT_POLICY.ot, min_threshold_min=raw['otMinThresholdMin'], rounding_min=raw['otRoundingMin'],
        require_preapproval=raw['requirePreapproval'], cap_day_min=raw['otCapDayMin'], cap_week_min=raw['otCapWeekMin'],
        night_window_enabled=raw['nightWindowEnabled'], night_premium_rate=raw['nightPremiumRate']))
    ids = b.get('employeeIds')
    emps = rows("""SELECT id, gender, ot_eligible, weekly_off_days, company_id, join_date, exit_date FROM employee
                    WHERE company_id = %s AND status = 'active' AND (%s::uuid[] IS NULL OR id = ANY(%s::uuid[]))
                    ORDER BY emp_code LIMIT 200""", [user.company_id, ids, ids])
    counts = {'late': 0, 'halfDay': 0, 'absent': 0, 'otDetected': 0, 'otMinutes': 0}
    affected: dict[str, int] = {}
    inputs = repo.load_range_inputs(user.company_id, emps, dates) if emps else {}
    for e in emps:
        eid = str(e['id'])
        for d in dates:
            x = inputs[f'{eid}|{d}']
            employed = (not e['join_date'] or d >= repo.iso(e['join_date'])) and (not e['exit_date'] or d <= repo.iso(e['exit_date']))
            if not x.shift or not employed:
                continue
            ctx = DayContext(holiday=x.holiday, weekly_off=weekday_of(d) in (e['weekly_off_days'] or []), leave=x.leave,
                             wfh=x.wfh, ot_eligible=e['ot_eligible'], ot_preapproved_min=x.ot_preapproved_min,
                             ot_minutes_this_week=x.ot_minutes_this_week,
                             short_leave=tuple(x.short_leave) if x.short_leave else None)
            day = compute_day(ComputeDayInput(employee_id=eid, local_date=d, shift=x.shift, punches=tuple(x.punches),
                                              ctx=ctx, policy=proposed))
            if day.late_min > 0:
                counts['late'] += 1
            if day.status == 'HalfDay':
                counts['halfDay'] += 1
            if day.status == 'Absent':
                counts['absent'] += 1
            if day.ot.minutes > 0:
                counts['otDetected'] += 1
                counts['otMinutes'] += day.ot.minutes
            # "Affected" means the outcome would change, not the hash (the preview policy always hashes differently).
            st = x.stored
            if st and (st['status'] != day.status or st['late_min'] != day.late_min or st['ot_min'] != day.ot.minutes):
                affected[eid] = affected.get(eid, 0) + 1
    c = one("""SELECT count(*) FILTER (WHERE v.late_min > 0)::int AS late, count(*) FILTER (WHERE v.status = 'HalfDay')::int AS half,
                      count(*) FILTER (WHERE v.status = 'Absent')::int AS absent, count(*) FILTER (WHERE v.ot_min > 0)::int AS ot,
                      coalesce(sum(v.ot_min), 0)::int AS otmin
                 FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id
                WHERE e.company_id = %s AND v.local_date BETWEEN %s AND %s AND (%s::uuid[] IS NULL OR e.id = ANY(%s::uuid[]))""",
            [user.company_id, b['from'], b['to'], ids, ids])
    current = {'late': c['late'], 'halfDay': c['half'], 'absent': c['absent'], 'otDetected': c['ot'], 'otMinutes': c['otmin']}
    f, t = b['from'], b['to']
    return {'range': {'from': f, 'to': t, 'days': len(dates)}, 'employees': len(emps), 'proposed': counts, 'current': current,
            'affectedEmployees': len(affected),
            'summary': (f"Applying to {f}..{t}: {counts['late']} late marks (was {current['late']}), {counts['halfDay']} half-days "
                        f"(was {current['halfDay']}), {counts['absent']} absents (was {current['absent']}), OT on "
                        f"{counts['otDetected']} days (was {current['otDetected']}).")}


# ---------------------------------------------------------------- branches

BRANCH_CREATE = Obj({'code': Str(min=1, max=20), 'nameEn': Str(min=1, max=120), 'nameNe': Str(max=120, optional=True)})
BRANCH_PATCH = Obj({'code': Str(min=1, max=20, optional=True), 'nameEn': Str(min=1, max=120, optional=True),
                    'nameNe': Str(max=120, nullish=True)})


def list_branches(user: AuthUser) -> list[dict]:
    _hr(user)
    return rows('SELECT id, code, name_en, name_ne, tz_offset_min, created_at FROM branch WHERE company_id = %s ORDER BY code',
                [user.company_id])


@transaction.atomic
def create_branch(user: AuthUser, body: dict) -> dict:
    _hr(user)
    b = parse(BRANCH_CREATE, body)
    row = one('INSERT INTO branch (company_id, code, name_en, name_ne) VALUES (%s, %s, %s, %s) RETURNING id, code, name_en, name_ne',
              [user.company_id, b['code'], b['nameEn'], b.get('nameNe')])
    audit('create', 'branch', actor_id=user.id, row_id=str(row['id']), after=b)
    return row


@transaction.atomic
def update_branch(user: AuthUser, branch_id: str, body: dict) -> dict:
    _hr(user)
    parse(Uuid(), branch_id)
    b = parse(BRANCH_PATCH, body)
    row = one("""UPDATE branch SET code = coalesce(%s, code), name_en = coalesce(%s, name_en),
                        name_ne = CASE WHEN %s THEN %s ELSE name_ne END
                  WHERE id = %s AND company_id = %s RETURNING id, code, name_en, name_ne""",
              [b.get('code'), b.get('nameEn'), 'nameNe' in b, b.get('nameNe'), branch_id, user.company_id])
    if not row:
        raise NotFoundError('branch not found')
    audit('update', 'branch', actor_id=user.id, row_id=str(branch_id), after=b)
    return row


# ---------------------------------------------------------------- org units

ORG_CREATE = Obj({'kind': Enum(('department', 'team')), 'code': Str(min=1, max=20), 'nameEn': Str(min=1, max=120),
                  'nameNe': Str(max=120, optional=True), 'branchId': Uuid(optional=True), 'parentId': Uuid(optional=True)})
ORG_PATCH = Obj({'nameEn': Str(min=1, max=120, optional=True), 'nameNe': Str(max=120, nullish=True),
                 'branchId': Uuid(nullish=True), 'parentId': Uuid(nullish=True)})
_ORG_COLS = 'id, branch_id, parent_id, kind, code, name_en, name_ne'


def list_org_units(user: AuthUser) -> list[dict]:
    _hr(user)
    return rows(f'SELECT {_ORG_COLS} FROM org_unit WHERE company_id = %s ORDER BY code', [user.company_id])


def _check_org_refs(company_id: str, b: dict, self_id: str | None = None) -> None:
    """Branch and parent ids must belong to the caller's company, and a unit cannot be its own parent."""
    if b.get('branchId') and not one('SELECT 1 AS x FROM branch WHERE id = %s AND company_id = %s', [b['branchId'], company_id]):
        raise NotFoundError('branch not found')
    if b.get('parentId') and not one('SELECT 1 AS x FROM org_unit WHERE id = %s AND company_id = %s', [b['parentId'], company_id]):
        raise NotFoundError('parent unit not found')
    if self_id and b.get('parentId') == str(self_id):
        raise BadRequestError('an org unit cannot be its own parent')


@transaction.atomic
def create_org_unit(user: AuthUser, body: dict) -> dict:
    _hr(user)
    b = parse(ORG_CREATE, body)
    _check_org_refs(user.company_id, b)
    row = one(f"""INSERT INTO org_unit (company_id, branch_id, parent_id, kind, code, name_en, name_ne)
                  VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_ORG_COLS}""",
              [user.company_id, b.get('branchId'), b.get('parentId'), b['kind'], b['code'], b['nameEn'], b.get('nameNe')])
    audit('create', 'org_unit', actor_id=user.id, row_id=str(row['id']), after=b)
    return row


@transaction.atomic
def update_org_unit(user: AuthUser, unit_id: str, body: dict) -> dict:
    _hr(user)
    parse(Uuid(), unit_id)
    b = parse(ORG_PATCH, body)
    _check_org_refs(user.company_id, b, unit_id)
    row = one(f"""UPDATE org_unit SET name_en = coalesce(%s, name_en), name_ne = CASE WHEN %s THEN %s ELSE name_ne END,
                         branch_id = CASE WHEN %s THEN %s::uuid ELSE branch_id END,
                         parent_id = CASE WHEN %s THEN %s::uuid ELSE parent_id END
                   WHERE id = %s AND company_id = %s RETURNING {_ORG_COLS}""",
              [b.get('nameEn'), 'nameNe' in b, b.get('nameNe'), 'branchId' in b, b.get('branchId'), 'parentId' in b,
               b.get('parentId'), unit_id, user.company_id])
    if not row:
        raise NotFoundError('org unit not found')
    audit('update', 'org_unit', actor_id=user.id, row_id=str(unit_id), after=b)
    return row
