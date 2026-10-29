"""
Company settings, branches and org units (port of apps/api/src/modules/settings/routes.ts). Everything but the header
profile is for HR and admin of the ACTIVE company; the functions check that themselves and scope every query to it.
"""
from __future__ import annotations

from dataclasses import replace

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from rest_framework import serializers

from engines.attendance import DEFAULT_POLICY, AttendancePolicy, ComputeDayInput, DayContext, compute_day
from engines.bs_calendar import BsRangeError, ad_to_bs, format_bs, weekday_of

from hajiri.attendance import repo
from hajiri.attendance.service import date_range, latest_days
from hajiri.core import settings_store
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError, ValidationError
from hajiri.core.validation import IsoDateField, parse_uuid, plain, validate
from hajiri.db.models import AttendanceDay, Branch, Company, CompanySetting, Employee, OrgUnit

from .fields import JsonString, plain_dict
from .schemas import OT_CAP_DAY_MIN, OT_CAP_WEEK_MIN, SETTING_DESCRIPTIONS, SETTING_KEYS, SETTING_SCHEMAS, policy_version

LOCKED = {'otCapDayMin': OT_CAP_DAY_MIN, 'otCapWeekMin': OT_CAP_WEEK_MIN,
          'note': 'Labour Act 2074 Sec 30(1): overtime caps cannot be raised above 4 h/day and 24 h/week.'}


def _hr(user: AuthUser) -> str:
    """HR or admin of the active company; returns that company's id."""
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr or admin')
    return company_id_of(user)


def get_setting(company_id: str, key: str) -> dict | None:
    """A stored setting value (None when the company never saved it)."""
    return settings_store.get_setting(company_id, key)


def parse_setting(key: str, body) -> dict:
    """A setting value checked against its serializer, with the defaults filled in."""
    return plain_dict(validate(SETTING_SCHEMAS[key], body))


def company_header(user: AuthUser) -> dict:
    """Name, logo and address of the ACTIVE company for the app header (any signed-in user), and today in AD and BS."""
    c = Company.objects.for_company(user.company_id).values('id', 'code', 'name_en', 'name_ne').first()
    if not c:
        raise NotFoundError('company not found')
    profile = get_setting(user.company_id, 'company.profile') or {}
    today_ad = npt_today().isoformat()
    try:
        today_bs = format_bs(ad_to_bs(today_ad))
    except BsRangeError:
        today_bs = None
    pick = lambda k, d: profile.get(k) if profile.get(k) is not None else d  # noqa: E731 - the TS `??`
    return {'company': {**c, 'nameEn': pick('nameEn', c['name_en']), 'nameNe': pick('nameNe', c['name_ne']),
                        'logoDataUrl': profile.get('logoDataUrl'), 'address': profile.get('address')},
            'today': {'ad': today_ad, 'bs': today_bs}}


def list_settings(user: AuthUser) -> dict:
    cid = _hr(user)
    stored = {r['key']: r for r in CompanySetting.objects.for_company(cid).values('key', 'value', 'updated_at', 'updated_by')}
    # An unsaved company profile defaults to the registered name, so it can be edited and saved back as is.
    company_name = Company.objects.for_company(cid).values_list('name_en', flat=True).first() or 'Company'
    out = []
    for key in SETTING_KEYS:
        row = stored.get(key)
        if row:
            value = row['value']
        else:
            try:
                value = parse_setting(key, {'nameEn': company_name} if key == 'company.profile' else {})
            except ValidationError:
                value = None
        out.append({'key': key, 'description': SETTING_DESCRIPTIONS[key], 'value': value,
                    'updatedAt': row['updated_at'] if row else None, 'updatedBy': row['updated_by'] if row else None})
    return {'settings': out, 'locked': LOCKED}


@transaction.atomic
def put_setting(user: AuthUser, key: str, body) -> dict:
    cid = _hr(user)
    if key not in SETTING_KEYS:
        raise NotFoundError(f'unknown setting {key}')
    parsed = parse_setting(key, body)
    if key == 'attendance.policy':
        parsed['version'] = policy_version(parsed)
    before = get_setting(cid, key)
    settings_store.put_setting(cid, key, parsed, updated_by=user.id)
    audit('update', 'company_setting', actor_id=user.id, row_id=key, before=before, after=parsed)
    return {'key': key, 'value': parsed}


# `from` is a Python keyword, so this serializer is built with type() (DRF collects the fields from the dict).
PreviewIn = type('PreviewIn', (serializers.Serializer,), {
    'policy': serializers.DictField(),
    'from': IsoDateField(),
    'to': IsoDateField(),
    'employeeIds': serializers.ListField(child=serializers.UUIDField(), max_length=200, required=False),
})


def _count_outcomes(qs) -> dict:
    """Late marks, half-days, absents and OT days (with their minutes) over attendance days."""
    c = qs.aggregate(late=Count('id', filter=Q(late_min__gt=0)),
                     halfDay=Count('id', filter=Q(status=AttendanceDay.Status.HALF_DAY)),
                     absent=Count('id', filter=Q(status=AttendanceDay.Status.ABSENT)),
                     otDetected=Count('id', filter=Q(ot_min__gt=0)), otMinutes=Coalesce(Sum('ot_min'), 0))
    return {k: c[k] for k in ('late', 'halfDay', 'absent', 'otDetected', 'otMinutes')}


def preview_attendance(user: AuthUser, body: dict) -> dict:
    """'Applying to last month': recompute in memory with a proposed attendance policy and compare late marks,
    half-days, absents and detected OT with what is stored. Writes nothing."""
    cid = _hr(user)
    b = plain(validate(PreviewIn, body))
    dates = date_range(b['from'], b['to'])
    if not dates or len(dates) > 31:
        raise BadRequestError('preview range must be 1 to 31 days')
    raw = parse_setting('attendance.policy', b['policy'])
    proposed = AttendancePolicy(version='preview', holiday_work=raw['holidayWork'], ot=replace(
        DEFAULT_POLICY.ot, min_threshold_min=raw['otMinThresholdMin'], rounding_min=raw['otRoundingMin'],
        require_preapproval=raw['requirePreapproval'], cap_day_min=raw['otCapDayMin'], cap_week_min=raw['otCapWeekMin'],
        night_window_enabled=raw['nightWindowEnabled'], night_premium_rate=raw['nightPremiumRate']))
    ids = b.get('employeeIds')
    people = Employee.objects.for_company(cid).filter(status=Employee.Status.ACTIVE)
    if ids is not None:
        people = people.filter(pk__in=ids)
    emps = list(people.order_by('emp_code').values('id', 'gender', 'ot_eligible', 'weekly_off_days', 'company_id',
                                                   'join_date', 'exit_date')[:200])
    counts = {'late': 0, 'halfDay': 0, 'absent': 0, 'otDetected': 0, 'otMinutes': 0}
    affected: dict[str, int] = {}
    inputs = repo.load_range_inputs(cid, emps, dates) if emps else {}
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
            if day.status == AttendanceDay.Status.HALF_DAY:
                counts['halfDay'] += 1
            if day.status == AttendanceDay.Status.ABSENT:
                counts['absent'] += 1
            if day.ot.minutes > 0:
                counts['otDetected'] += 1
                counts['otMinutes'] += day.ot.minutes
            # "Affected" means the outcome would change, not the hash (the preview policy always hashes differently).
            st = x.stored
            if st and (st['status'] != day.status or st['late_min'] != day.late_min or st['ot_min'] != day.ot.minutes):
                affected[eid] = affected.get(eid, 0) + 1
    stored = latest_days().for_company(cid).filter(local_date__range=(b['from'], b['to']))
    if ids is not None:
        stored = stored.filter(employee_id__in=ids)
    current = _count_outcomes(stored)
    f, t = b['from'], b['to']
    return {'range': {'from': f, 'to': t, 'days': len(dates)}, 'employees': len(emps), 'proposed': counts, 'current': current,
            'affectedEmployees': len(affected),
            'summary': (f"Applying to {f}..{t}: {counts['late']} late marks (was {current['late']}), {counts['halfDay']} half-days "
                        f"(was {current['halfDay']}), {counts['absent']} absents (was {current['absent']}), OT on "
                        f"{counts['otDetected']} days (was {current['otDetected']}).")}


# ---------------------------------------------------------------- branches

class BranchIn(serializers.Serializer):
    code = JsonString(min_length=1, max_length=20, allow_blank=False)
    nameEn = JsonString(min_length=1, max_length=120, allow_blank=False)
    nameNe = JsonString(max_length=120, required=False)


class BranchPatchIn(serializers.Serializer):
    code = JsonString(min_length=1, max_length=20, allow_blank=False, required=False)
    nameEn = JsonString(min_length=1, max_length=120, allow_blank=False, required=False)
    nameNe = JsonString(max_length=120, required=False, allow_null=True)


BRANCH_FIELDS = ('id', 'code', 'name_en', 'name_ne')


def list_branches(user: AuthUser) -> list[dict]:
    # Attendance admins read the list too: the kiosk set-up (Attendance -> Kiosks) ties each kiosk to a branch.
    if not has_role(user, 'hr', 'attendance_admin'):
        raise ForbiddenError('requires role hr or admin')
    cid = company_id_of(user)
    return list(Branch.objects.for_company(cid).order_by('code').values(*BRANCH_FIELDS, 'tz_offset_min', 'created_at'))


@transaction.atomic
def create_branch(user: AuthUser, body: dict) -> dict:
    cid = _hr(user)
    b = validate(BranchIn, body)
    br = Branch.objects.create(company_id=cid, code=b['code'], name_en=b['nameEn'], name_ne=b.get('nameNe'))
    audit('create', 'branch', actor_id=user.id, row_id=str(br.pk), after=b)
    return Branch.objects.filter(pk=br.pk).values(*BRANCH_FIELDS).get()


@transaction.atomic
def update_branch(user: AuthUser, branch_id: str, body: dict) -> dict:
    cid = _hr(user)
    parse_uuid(branch_id)
    b = validate(BranchPatchIn, body)
    changes = {'code': b.get('code'), 'name_en': b.get('nameEn')}
    changes = {k: v for k, v in changes.items() if v is not None}
    if 'nameNe' in b:
        changes['name_ne'] = b['nameNe']
    mine = Branch.objects.for_company(cid).filter(pk=branch_id)
    if not (mine.update(**changes) if changes else mine.exists()):
        raise NotFoundError('branch not found')
    audit('update', 'branch', actor_id=user.id, row_id=str(branch_id), after=b)
    return mine.values(*BRANCH_FIELDS).get()


# ---------------------------------------------------------------- org units

class OrgUnitIn(serializers.Serializer):
    kind = serializers.ChoiceField(OrgUnit.Kind.values)
    code = JsonString(min_length=1, max_length=20, allow_blank=False)
    nameEn = JsonString(min_length=1, max_length=120, allow_blank=False)
    nameNe = JsonString(max_length=120, required=False)
    branchId = serializers.UUIDField(required=False)
    parentId = serializers.UUIDField(required=False)


class OrgUnitPatchIn(serializers.Serializer):
    nameEn = JsonString(min_length=1, max_length=120, allow_blank=False, required=False)
    nameNe = JsonString(max_length=120, required=False, allow_null=True)
    branchId = serializers.UUIDField(required=False, allow_null=True)
    parentId = serializers.UUIDField(required=False, allow_null=True)


ORG_FIELDS = ('id', 'branch_id', 'parent_id', 'kind', 'code', 'name_en', 'name_ne')


def list_org_units(user: AuthUser) -> list[dict]:
    cid = _hr(user)
    return list(OrgUnit.objects.for_company(cid).order_by('code').values(*ORG_FIELDS))


def _check_org_refs(company_id: str, b: dict, self_id: str | None = None) -> None:
    """Branch and parent ids must belong to the active company, and a unit cannot be its own parent."""
    if b.get('branchId') and not Branch.objects.for_company(company_id).filter(pk=b['branchId']).exists():
        raise NotFoundError('branch not found')
    if b.get('parentId') and not OrgUnit.objects.for_company(company_id).filter(pk=b['parentId']).exists():
        raise NotFoundError('parent unit not found')
    if self_id and b.get('parentId') == str(self_id):
        raise BadRequestError('an org unit cannot be its own parent')


@transaction.atomic
def create_org_unit(user: AuthUser, body: dict) -> dict:
    cid = _hr(user)
    b = plain(validate(OrgUnitIn, body))
    _check_org_refs(cid, b)
    unit = OrgUnit.objects.create(company_id=cid, branch_id=b.get('branchId'), parent_id=b.get('parentId'), kind=b['kind'],
                                  code=b['code'], name_en=b['nameEn'], name_ne=b.get('nameNe'))
    audit('create', 'org_unit', actor_id=user.id, row_id=str(unit.pk), after=b)
    return OrgUnit.objects.filter(pk=unit.pk).values(*ORG_FIELDS).get()


@transaction.atomic
def update_org_unit(user: AuthUser, unit_id: str, body: dict) -> dict:
    cid = _hr(user)
    parse_uuid(unit_id)
    b = plain(validate(OrgUnitPatchIn, body))
    _check_org_refs(cid, b, unit_id)
    changes = {'name_en': b['nameEn']} if b.get('nameEn') is not None else {}
    for key, col in (('nameNe', 'name_ne'), ('branchId', 'branch_id'), ('parentId', 'parent_id')):
        if key in b:
            changes[col] = b[key]
    mine = OrgUnit.objects.for_company(cid).filter(pk=unit_id)
    if not (mine.update(**changes) if changes else mine.exists()):
        raise NotFoundError('org unit not found')
    audit('update', 'org_unit', actor_id=user.id, row_id=str(unit_id), after=b)
    return mine.values(*ORG_FIELDS).get()
