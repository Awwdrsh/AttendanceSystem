"""Port of apps/api/test/settings.test.ts, plus seed-defaults and the policy version."""
import re

import pytest
from django.db import IntegrityError, transaction

from hajiri.company_settings import service
from hajiri.company_settings.schemas import policy_version
from hajiri.company_settings.seed import create_company, seed_defaults
from tests.support.sql import one, rows
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError, ValidationError


@pytest.fixture
def hr(login_as):
    return login_as(['hr']).user


@pytest.fixture
def emp(login_as, demo):
    return login_as(['employee'], demo['sita']).user


def test_lists_all_keys_with_descriptions_and_locked_caps(hr, emp):
    r = service.list_settings(hr)
    assert 'attendance.policy' in [s['key'] for s in r['settings']]
    assert r['locked']['otCapDayMin'] == 240 and r['locked']['otCapWeekMin'] == 1440
    profile = next(s for s in r['settings'] if s['key'] == 'company.profile')
    assert profile['value']['nameEn'] == 'Demo Company' and profile['updatedAt'] is None
    with pytest.raises(ForbiddenError):
        service.list_settings(emp)


def test_clamps_ot_caps_bumps_the_policy_version_and_audits(hr):
    r = service.put_setting(hr, 'attendance.policy', {'otMinThresholdMin': 20, 'otCapDayMin': 600, 'otCapWeekMin': 9999})
    v = r['value']
    assert (v['otMinThresholdMin'], v['otCapDayMin'], v['otCapWeekMin']) == (20, 240, 1440)
    assert re.fullmatch(r'v-[0-9a-f]{12}', v['version'])
    r2 = service.put_setting(hr, 'attendance.policy', {'otMinThresholdMin': 25})
    assert r2['value']['version'] != v['version']
    a = one("SELECT before, after FROM audit_log WHERE table_name = 'company_setting' AND row_id = 'attendance.policy' ORDER BY id DESC LIMIT 1")
    assert a['before']['otMinThresholdMin'] == 20
    assert a['after']['otMinThresholdMin'] == 25


def test_policy_version_matches_the_ts_algorithm():
    # JSON.stringify(rest, Object.keys(rest).sort()) keeps nested keys only when they are also top-level keys, so the
    # latePenalty object hashes as {}. Value computed by the TS policyVersion for the schema defaults.
    defaults = {'otMinThresholdMin': 30, 'otRoundingMin': 15, 'requirePreapproval': True, 'otCapDayMin': 240,
                'otCapWeekMin': 1440, 'holidayWork': 'substitute_leave', 'nightWindowEnabled': False, 'nightPremiumRate': 0,
                'autoClockOutAfterMin': 0, 'regularisationBackdateDays': 30, 'regularisationMonthlyCap': 5,
                'wfhPhotoRequired': True, 'wfhPhotoRetentionDays': 90,
                'latePenalty': {'enabled': False, 'lateCount': 3, 'deductionDays': 0.5}}
    assert policy_version(defaults) == 'v-a82b80b59ab0'
    assert policy_version({**defaults, 'nightPremiumRate': 0.25}) == 'v-a14d45df16a2'
    assert policy_version(defaults) == policy_version({**defaults, 'version': 'anything'})
    assert policy_version(defaults) == policy_version({**defaults, 'latePenalty': {'enabled': True}})
    assert policy_version(defaults) != policy_version({**defaults, 'otMinThresholdMin': 31})


def test_rejects_unknown_keys_and_invalid_values(hr):
    with pytest.raises(NotFoundError, match='unknown setting nope'):
        service.put_setting(hr, 'nope', {})
    with pytest.raises(ValidationError):
        service.put_setting(hr, 'notifications.policy', {'quietHoursStart': '25:00'})
    with pytest.raises(ValidationError):
        service.put_setting(hr, 'company.profile', {'nameEn': 'X', 'pan': '12345'})


def test_company_profile_and_header(hr, emp):
    service.put_setting(hr, 'company.profile', {'nameEn': 'Demo Pvt Ltd', 'nameNe': 'डेमो', 'pan': '123456789'})
    r = service.company_header(emp)
    assert r['company']['nameEn'] == 'Demo Pvt Ltd' and r['company']['nameNe'] == 'डेमो'
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}', r['today']['bs'])


def test_security_policy_dedupes_roles(hr):
    r = service.put_setting(hr, 'security.policy', {'require2faRoles': ['hr', 'hr', 'admin']})
    assert r['value'] == {'require2faRoles': ['hr', 'admin']}


def test_branches_and_org_units(hr, demo):
    b = service.create_branch(hr, {'code': 'PKR', 'nameEn': 'Pokhara'})
    with pytest.raises(IntegrityError), transaction.atomic():
        service.create_branch(hr, {'code': 'PKR', 'nameEn': 'Dup'})
    u = service.create_org_unit(hr, {'kind': 'department', 'code': 'FIN', 'nameEn': 'Finance', 'branchId': str(b['id'])})
    p = service.update_org_unit(hr, str(u['id']), {'nameNe': 'वित्त'})
    assert p['name_ne'] == 'वित्त' and p['name_en'] == 'Finance'
    assert len(service.list_branches(hr)) == 2
    with pytest.raises(BadRequestError, match='own parent'):
        service.update_org_unit(hr, str(u['id']), {'parentId': str(u['id'])})
    with pytest.raises(NotFoundError, match='branch not found'):
        service.create_org_unit(hr, {'kind': 'team', 'code': 'T', 'nameEn': 'T', 'branchId': '00000000-0000-7000-8000-0000000000ff'})
    upd = service.update_branch(hr, str(b['id']), {'nameNe': None, 'nameEn': 'Pokhara Valley'})
    assert upd['name_en'] == 'Pokhara Valley' and upd['code'] == 'PKR'
    a = one("SELECT count(*)::int AS n FROM audit_log WHERE table_name IN ('branch', 'org_unit')")
    assert a['n'] == 4


def test_another_companys_branch_cannot_be_changed(hr):
    other = one("INSERT INTO company (code, name_en) VALUES ('OTH', 'Other') RETURNING id")['id']
    ob = one("INSERT INTO branch (company_id, code, name_en) VALUES (%s, 'X', 'X') RETURNING id", [other])['id']
    with pytest.raises(NotFoundError):
        service.update_branch(hr, str(ob), {'nameEn': 'Mine now'})
    with pytest.raises(NotFoundError, match='branch not found'):
        service.create_org_unit(hr, {'kind': 'team', 'code': 'T', 'nameEn': 'T', 'branchId': str(ob)})


def test_preview_reports_proposed_vs_current_without_writing(hr):
    before = one('SELECT count(*)::int AS n FROM attendance_day')['n']
    r = service.preview_attendance(hr, {'policy': {'otMinThresholdMin': 0}, 'from': '2026-09-21', 'to': '2026-09-22'})
    assert r['range']['days'] == 2 and r['employees'] == 2
    assert 'late marks' in r['summary']
    assert one('SELECT count(*)::int AS n FROM attendance_day')['n'] == before
    with pytest.raises(BadRequestError):
        service.preview_attendance(hr, {'policy': {}, 'from': '2026-01-01', 'to': '2026-12-31'})


def test_preview_matches_the_recompute_engine(hr, demo):
    from hajiri.attendance.service import recompute_day
    from tests.core.punches import insert_punch
    insert_punch(demo['sita'], '2026-09-21', '10:30')
    insert_punch(demo['sita'], '2026-09-21', '19:00')
    recompute_day(demo['sita'], '2026-09-21')
    r = service.preview_attendance(hr, {'policy': {}, 'from': '2026-09-21', 'to': '2026-09-21', 'employeeIds': [demo['sita']]})
    assert r['proposed']['late'] == r['current']['late'] == 1
    assert r['affectedEmployees'] == 0


def test_seed_defaults_is_idempotent(db):
    c = create_company('NEWCO', 'New Co Pvt Ltd')
    assert c['seeded']['leaveTypes'] == 9 and c['seeded']['workflows'] == 14 and c['seeded']['settings'] == 9
    again = seed_defaults(c['id'])
    assert (again['leaveTypes'], again['workflows'], again['settings']) == (0, 0, 0)
    hourly = rows("SELECT code FROM leave_type WHERE company_id = %s AND allow_hourly ORDER BY code", [c['id']])
    assert [r['code'] for r in hourly] == ['HOME', 'SICK']
    ev = {r['code']: r['event_days'] for r in rows("SELECT code, event_days FROM leave_type WHERE company_id = %s AND event_days IS NOT NULL", [c['id']])}
    assert ev == {'MATERNITY': 60, 'PATERNITY': 15, 'MOURNING': 13}
    wf = one("SELECT steps FROM workflow_definition WHERE company_id = %s AND object_type = 'resignation'", [c['id']])
    assert wf['steps'] == [{'role': 'manager'}, {'role': 'hr'}]


def test_registered_module_seeders_run(db, monkeypatch):
    from hajiri.worker import registry
    monkeypatch.setitem(registry._SEEDERS, 'demo', registry.Seeder('demo', lambda cid: {'demoThings': 2}))
    c = create_company('SEEDCO', 'Seed Co')
    assert c['seeded']['demoThings'] == 2
