"""Policy templates copied between companies of the group: preview, conflict choice, apply, audit."""
from datetime import time, timedelta
from decimal import Decimal

import pytest

from hajiri.company_settings.seed import seed_defaults
from hajiri.core import settings_store
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError, ValidationError
from hajiri.db.models import (ApprovalFlow, AuditLog, Holiday, LeaveType, SalaryComponent, SalaryStructure,
                              SalaryStructureVersion, Shift, WorkflowDefinition)
from hajiri.group_admin import services

from .conftest import actor


@pytest.fixture
def seeded(world):
    seed_defaults(str(world.alpha.id))
    return world


def _body(world, sections, on_conflict='skip', targets=None):
    return {'sourceCompanyId': str(world.alpha.id), 'targetCompanyIds': targets or [str(world.beta.id)],
            'sections': sections, 'onConflict': on_conflict}


def _actions(result, section):
    t = result['targets'][0]
    s = next(s for s in t['sections'] if s['key'] == section)
    return {i['key']: i['action'] for i in s['items']}


def test_seeded_companies_have_the_same_leave_types(seeded, admin):
    acts = _actions(services.preview_policies(admin, _body(seeded, ['leave_types'])), 'leave_types')
    assert acts and set(acts.values()) == {'same'}


def test_preview_changes_nothing_and_apply_creates_and_overwrites(seeded, admin):
    w = seeded
    LeaveType.objects.for_company(w.alpha.id).filter(code='CASUAL').update(accrual_days=Decimal('8'), max_balance=Decimal('8'))
    LeaveType.objects.create(company=w.alpha, code='STUDY', name_en='Study leave', paid=False, club_with=None)
    p = services.preview_policies(admin, _body(w, ['leave_types']))
    acts = _actions(p, 'leave_types')
    assert acts['CASUAL'] == 'keep' and acts['STUDY'] == 'create' and acts['HOME'] == 'same'
    assert not LeaveType.objects.for_company(w.beta.id).filter(code='STUDY').exists()
    # Keep: only what is missing is created.
    services.copy_policies(admin, _body(w, ['leave_types']))
    assert LeaveType.objects.for_company(w.beta.id).get(code='STUDY').paid is False
    assert LeaveType.objects.for_company(w.beta.id).get(code='CASUAL').accrual_days == Decimal('6')
    # Overwrite: the conflict takes the source's values.
    p = services.preview_policies(admin, _body(w, ['leave_types'], 'overwrite'))
    assert _actions(p, 'leave_types')['CASUAL'] == 'overwrite' and _actions(p, 'leave_types')['STUDY'] == 'same'
    r = services.copy_policies(admin, _body(w, ['leave_types'], 'overwrite'))
    assert r['totals']['overwrite'] == 1
    casual = LeaveType.objects.for_company(w.beta.id).get(code='CASUAL')
    assert casual.accrual_days == Decimal('8') and casual.max_balance == Decimal('8')
    entry = AuditLog.objects.filter(action='update', table_name='leave_type', row_id=str(casual.id)).latest('id')
    assert entry.before['accrual_days'] == '6.00' and entry.after['copiedFrom'] == str(w.alpha.id)
    assert AuditLog.objects.filter(action='copy_policies', row_id=str(w.beta.id)).count() == 2


def test_clubbed_leave_types_point_at_the_target_company(seeded, admin):
    w = seeded
    home = LeaveType.objects.for_company(w.alpha.id).get(code='HOME')
    LeaveType.objects.create(company=w.alpha, code='EXTRA', name_en='Extra leave', club_with=home)
    services.copy_policies(admin, _body(w, ['leave_types']))
    extra = LeaveType.objects.for_company(w.beta.id).get(code='EXTRA')
    assert extra.club_with.company_id == w.beta.id and extra.club_with.code == 'HOME'


def test_shifts_holidays_components_and_structures(seeded, admin):
    w = seeded
    Shift.objects.create(company=w.alpha, code='NIGHT', name_en='Night', start_time=time(22), end_time=time(6))
    soon = npt_today() + timedelta(days=20)
    Holiday.objects.create(company=w.alpha, local_date=soon, name_en='Company day')
    Holiday.objects.create(company=w.alpha, local_date=npt_today() - timedelta(days=20), name_en='Past day')
    SalaryComponent.objects.create(company=w.alpha, code='FUEL', name_en='Fuel allowance', type='earning', formula='2000')
    s = SalaryStructure.objects.create(company=w.alpha, code='SALES', name_en='Sales staff')
    SalaryStructureVersion.objects.create(structure=s, version=1, components=[{'code': 'BASIC'}, {'code': 'FUEL'}])
    # Without the components, a structure that uses a missing one is not copied.
    p = services.preview_policies(admin, _body(w, ['salary_structures']))
    t = next(x for x in p['targets'][0]['sections'] if x['key'] == 'salary_structures')
    sales = next(i for i in t['items'] if i['key'] == 'SALES')
    assert sales['action'] == 'skip' and 'FUEL' in sales['reason']
    r = services.copy_policies(admin, _body(w, ['shifts', 'holidays', 'salary_components', 'salary_structures']))
    assert Shift.objects.for_company(w.beta.id).get(code='NIGHT').start_time == time(22)
    names = set(Holiday.objects.for_company(w.beta.id).values_list('name_en', flat=True))
    assert 'Company day' in names and 'Past day' not in names
    assert SalaryComponent.objects.for_company(w.beta.id).get(code='FUEL').formula == '2000'
    v = SalaryStructureVersion.objects.get(structure__company=w.beta, structure__code='SALES')
    assert v.version == 1 and v.components == [{'code': 'BASIC'}, {'code': 'FUEL'}]
    assert r['totals']['create'] >= 4


def test_statutory_components_are_never_overwritten(seeded, admin):
    w = seeded
    comp = SalaryComponent.objects.for_company(w.alpha.id).filter(statutory=True).first()
    SalaryComponent.objects.for_company(w.alpha.id).filter(code=comp.code).update(formula='1')
    acts = _actions(services.preview_policies(admin, _body(w, ['salary_components'], 'overwrite')), 'salary_components')
    assert acts[comp.code] == 'skip'
    services.copy_policies(admin, _body(w, ['salary_components'], 'overwrite'))
    assert SalaryComponent.objects.for_company(w.beta.id).get(code=comp.code).formula != '1'


def test_a_frozen_structure_gets_a_new_version(seeded, admin):
    w = seeded
    for c in (w.alpha, w.beta):
        s = SalaryStructure.objects.create(company=c, code='OPS', name_en='Operations')
        SalaryStructureVersion.objects.create(structure=s, version=1, components=[{'code': 'BASIC'}])
    from django.db.models.functions import Now
    SalaryStructureVersion.objects.filter(structure__company=w.beta, structure__code='OPS').update(frozen_at=Now())
    SalaryStructureVersion.objects.filter(structure__company=w.alpha, structure__code='OPS').update(
        components=[{'code': 'BASIC'}, {'code': 'GRADE'}])
    SalaryComponent.objects.get_or_create(company=w.beta, code='GRADE', defaults={'name_en': 'Grade', 'type': 'earning'})
    SalaryComponent.objects.get_or_create(company=w.alpha, code='GRADE', defaults={'name_en': 'Grade', 'type': 'earning'})
    services.copy_policies(admin, _body(w, ['salary_structures'], 'overwrite'))
    versions = list(SalaryStructureVersion.objects.filter(structure__company=w.beta, structure__code='OPS')
                    .order_by('version').values_list('version', 'components'))
    assert versions == [(1, [{'code': 'BASIC'}]), (2, [{'code': 'BASIC'}, {'code': 'GRADE'}])]


def test_approval_flows_naming_people_are_not_copied(seeded, admin):
    w = seeded
    WorkflowDefinition.objects.for_company(w.alpha.id).filter(object_type='leave_request').update(
        steps=[{'kind': 'user', 'userId': w.admin_a.id}])
    WorkflowDefinition.objects.for_company(w.alpha.id).filter(object_type='ot_request').update(
        steps=[{'role': 'manager'}, {'role': 'hr'}])
    ApprovalFlow.objects.create(company=w.alpha, object_type='expense_claim', name='Big claims', steps=[{'role': 'hr'}],
                                priority=5)
    ApprovalFlow.objects.create(company=w.alpha, object_type='expense_claim', name='Sales team', steps=[{'role': 'hr'}],
                                conditions={'branchIds': ['01a00000-0000-7000-8000-000000000001']})
    acts = _actions(services.preview_policies(admin, _body(w, ['approval_flows'], 'overwrite')), 'approval_flows')
    assert acts['default:leave_request'] == 'skip' and acts['default:ot_request'] == 'overwrite'
    assert acts['flow:expense_claim:Big claims'] == 'create' and acts['flow:expense_claim:Sales team'] == 'skip'
    services.copy_policies(admin, _body(w, ['approval_flows'], 'overwrite'))
    assert WorkflowDefinition.objects.for_company(w.beta.id).get(object_type='ot_request').steps == [{'role': 'manager'}, {'role': 'hr'}]
    assert WorkflowDefinition.objects.for_company(w.beta.id).get(object_type='leave_request').steps == [{'role': 'manager'}]
    assert ApprovalFlow.objects.for_company(w.beta.id).filter(name='Big claims', priority=5).exists()
    assert not ApprovalFlow.objects.for_company(w.beta.id).filter(name='Sales team').exists()


def test_settings_sections_and_leave_type_ids_are_remapped(seeded, admin):
    w = seeded
    alpha_home = LeaveType.objects.for_company(w.alpha.id).get(code='HOME')
    extras = settings_store.get_setting(w.alpha.id, 'attendance.extras')
    extras['latePenalty'] = {**extras['latePenalty'], 'enabled': True, 'leaveTypeIds': [str(alpha_home.id)]}
    settings_store.put_setting(w.alpha.id, 'attendance.extras', extras)
    policy = {**settings_store.get_setting(w.alpha.id, 'leave.policy'), 'maxContinuousDays': 20}
    settings_store.put_setting(w.alpha.id, 'leave.policy', policy)
    services.copy_policies(admin, _body(w, ['setting:attendance.extras', 'setting:leave.policy'], 'overwrite'))
    beta_home = LeaveType.objects.for_company(w.beta.id).get(code='HOME')
    copied = settings_store.get_setting(w.beta.id, 'attendance.extras')
    assert copied['latePenalty']['enabled'] is True and copied['latePenalty']['leaveTypeIds'] == [str(beta_home.id)]
    assert settings_store.get_setting(w.beta.id, 'leave.policy')['maxContinuousDays'] == 20
    assert 'setting:company.profile' not in services.SECTION_KEYS


def test_nothing_targets_a_company_outside_the_group(seeded, admin):
    w = seeded
    with pytest.raises(NotFoundError, match='company not found'):
        services.preview_policies(admin, _body(w, ['leave_types'], targets=[str(w.gamma.id)]))
    with pytest.raises(NotFoundError, match='company not found'):
        services.copy_policies(admin, {**_body(w, ['leave_types']), 'sourceCompanyId': str(w.gamma.id)})
    with pytest.raises(NotFoundError):
        services.copy_policies(admin, _body(w, ['leave_types'], targets=[str(w.beta.id), str(w.gamma.id)]))
    assert not LeaveType.objects.for_company(w.gamma.id).exists()
    with pytest.raises(BadRequestError, match='other than the source'):
        services.preview_policies(admin, _body(w, ['leave_types'], targets=[str(w.alpha.id)]))
    with pytest.raises(ValidationError):
        services.preview_policies(admin, _body(w, ['setting:company.profile']))
    for person in (w.admin_b, w.ghr):
        with pytest.raises(ForbiddenError):
            services.copy_policies(actor(w, person), _body(w, ['leave_types']))
