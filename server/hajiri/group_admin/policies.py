"""
Policy templates across the group (/group/policies), for group admins: copy leave types, salary components and
structures, approval flows, holidays, shifts and company settings sections from one company of the group to
others.

`preview` says, per target company and section, what would be created, overwritten, kept (exists and differs, and the
conflict choice is skip), already the same, or skipped (cannot be copied, with the reason). `apply` does exactly
that in one transaction and audits every row it writes, plus one 'copy_policies' entry per target company.

Rows are matched by their natural key (leave type, component, structure and shift code; flow object type and name;
holiday date and name; setting key). Anything that refers to people, branches or departments of the source company
(approval steps naming a person or group, conditional flows, branch holidays) is not copied, since those ids mean
nothing in another company.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from django.db import transaction
from django.db.models import F, Max, OuterRef, Subquery
from django.db.models.functions import Now

from hajiri.core import settings_store
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError
from hajiri.core.validation import plain, validate
from hajiri.db.models import (ApprovalFlow, Holiday, LeaveType, SalaryComponent, SalaryStructure,
                              SalaryStructureVersion, Shift, WorkflowDefinition)

from .common import group_scope
from .serializers import PolicyCopyIn

# Settings sections that are policies (the company profile is the company's identity and is never copied).
SETTING_SECTIONS = {
    'attendance.policy': 'Attendance policy',
    'attendance.extras': 'Attendance rules (late penalties, clock-in blocking)',
    'leave.policy': 'Leave policy',
    'payroll.policy': 'Payroll policy',
    'payslip.policy': 'Payslip settings',
    'notifications.policy': 'Notification timing',
    'security.policy': 'Sign-in security',
    'time.policy': 'Timesheet policy',
    'hiring.policy': 'Hiring policy',
    'people.policy': 'People records policy',
}


@dataclass
class Item:
    key: str
    label: str
    values: dict
    skip: str | None = None


@dataclass
class Target:
    """What one target company already has, filled by the sections as they are planned."""
    company_id: str
    name: str
    component_codes: set = field(default_factory=set)
    leave_types: dict = field(default_factory=dict)        # code -> id


def _object_label(object_type: str) -> str:
    return object_type.replace('_', ' ').capitalize()


class Section:
    key = ''
    label = ''
    table = ''

    def items(self, cid: str) -> list[Item]:
        raise NotImplementedError

    def existing(self, cid: str) -> dict[str, dict]:
        return {i.key: i.values for i in self.items(cid)}

    def adapt(self, item: Item, target: Target) -> Item:
        """The source item as it would be in the target company (ids remapped); may set item.skip."""
        return item

    def locked(self, key: str, current: dict) -> str | None:
        """Why an existing row of the target must not be overwritten, or None."""
        return None

    def create(self, target: Target, item: Item) -> str:
        raise NotImplementedError

    def overwrite(self, target: Target, item: Item) -> str:
        raise NotImplementedError

    def after_apply(self, target: Target, items: list[Item]) -> None:
        pass


# ---------------------------------------------------------------- leave types

class LeaveTypes(Section):
    key, label, table = 'leave_types', 'Leave types', 'leave_type'
    FIELDS = ('code', 'name_en', 'name_ne', 'paid', 'statutory', 'accrual_basis', 'accrual_days', 'accrual_per_worked_days',
              'max_balance', 'carry_forward', 'encashable', 'certificate_after_days', 'allow_half_day', 'max_consecutive_days',
              'min_service_days', 'negative_balance_limit', 'gender_scope', 'sandwich_rule', 'expiry_days', 'sort_order',
              'event_days', 'allow_hourly')

    def items(self, cid):
        rows = LeaveType.objects.for_company(cid).order_by('sort_order', 'code').values(*self.FIELDS, club=F('club_with__code'))
        return [Item(r['code'], f"{r['name_en']} ({r['code']})", {k: v for k, v in r.items() if k != 'code'}) for r in rows]

    def _fields(self, item):
        return {k: v for k, v in item.values.items() if k != 'club'}

    def create(self, target, item):
        lt = LeaveType.objects.create(company_id=target.company_id, code=item.key, **self._fields(item))
        target.leave_types[item.key] = str(lt.pk)
        return str(lt.pk)

    def overwrite(self, target, item):
        qs = LeaveType.objects.for_company(target.company_id).filter(code=item.key)
        qs.update(**self._fields(item))
        return str(qs.values_list('pk', flat=True).get())

    def after_apply(self, target, items):
        """'Clubbed with' points at another leave type by code; resolved once every type exists."""
        codes = dict(LeaveType.objects.for_company(target.company_id).values_list('code', 'pk'))
        for i in items:
            (LeaveType.objects.for_company(target.company_id).filter(code=i.key)
             .update(club_with_id=codes.get(i.values.get('club'))))


# ---------------------------------------------------------------- salary components and structures

class SalaryComponents(Section):
    key, label, table = 'salary_components', 'Salary components', 'salary_component'
    FIELDS = ('code', 'name_en', 'name_ne', 'type', 'formula', 'condition', 'depends_on_payment_days', 'taxable', 'statutory',
              'rounding', 'sort_order', 'active')

    def items(self, cid):
        rows = SalaryComponent.objects.for_company(cid).order_by('sort_order', 'code').values(*self.FIELDS)
        return [Item(r['code'], f"{r['name_en']} ({r['code']})", {k: v for k, v in r.items() if k != 'code'}) for r in rows]

    def locked(self, key, current):
        return 'statutory components are locked' if current.get('statutory') else None

    def adapt(self, item, target):
        if item.values.get('statutory') and item.key not in target.component_codes:
            item.skip = 'statutory components come with the payroll rules of each company'
        return item

    def create(self, target, item):
        SalaryComponent.objects.create(company_id=target.company_id, code=item.key, **item.values)
        return item.key

    def overwrite(self, target, item):
        SalaryComponent.objects.for_company(target.company_id).filter(code=item.key).update(**item.values)
        return item.key


class SalaryStructures(Section):
    key, label, table = 'salary_structures', 'Salary structures', 'salary_structure'

    def items(self, cid):
        latest = SalaryStructureVersion.objects.filter(structure=OuterRef('pk')).order_by('-version')
        rows = (SalaryStructure.objects.for_company(cid).order_by('code')
                .annotate(components=Subquery(latest.values('components')[:1])).values('code', 'name_en', 'components'))
        return [Item(r['code'], f"{r['name_en']} ({r['code']})", {'name_en': r['name_en'], 'components': r['components'] or []})
                for r in rows]

    def adapt(self, item, target):
        missing = sorted({c.get('code') for c in item.values['components']} - target.component_codes)
        if missing:
            item.skip = f"uses components {target.name} does not have: {', '.join(missing)}"
        return item

    def create(self, target, item):
        s = SalaryStructure.objects.create(company_id=target.company_id, code=item.key, name_en=item.values['name_en'])
        SalaryStructureVersion.objects.create(structure=s, version=1, components=item.values['components'])
        return str(s.pk)

    def overwrite(self, target, item):
        """A new version when the latest one is already in use (frozen); otherwise the latest version changes."""
        s = SalaryStructure.objects.for_company(target.company_id).select_for_update(no_key=True).get(code=item.key)
        if s.name_en != item.values['name_en']:
            SalaryStructure.objects.filter(pk=s.pk).update(name_en=item.values['name_en'])
        latest = SalaryStructureVersion.objects.filter(structure=s).order_by('-version').first()
        if latest is None or latest.frozen_at is not None:
            n = SalaryStructureVersion.objects.filter(structure=s).aggregate(n=Max('version'))['n'] or 0
            SalaryStructureVersion.objects.create(structure=s, version=n + 1, components=item.values['components'])
        elif latest.components != item.values['components']:
            SalaryStructureVersion.objects.filter(pk=latest.pk).update(components=item.values['components'])
        return str(s.pk)


# ---------------------------------------------------------------- approval flows

def _refers_to_people(steps, conditions=None) -> bool:
    if any(isinstance(s, dict) and (s.get('userId') or s.get('groupId')) for s in (steps or [])):
        return True
    return any(v for v in (conditions or {}).values())


class ApprovalFlows(Section):
    key, label, table = 'approval_flows', 'Approval flows', 'workflow_definition'

    def items(self, cid):
        out = []
        for r in WorkflowDefinition.objects.for_company(cid).order_by('object_type').values(
                'object_type', 'steps', 'sla_hours', 'escalation_hours'):
            ot = r.pop('object_type')
            out.append(Item(f'default:{ot}', f'{_object_label(ot)}: default chain', r,
                            'an approver is a person or group of this company' if _refers_to_people(r['steps']) else None))
        for r in ApprovalFlow.objects.for_company(cid).order_by('object_type', 'priority', 'name').values(
                'object_type', 'name', 'conditions', 'steps', 'priority', 'active'):
            ot, name = r.pop('object_type'), r.pop('name')
            out.append(Item(f'flow:{ot}:{name}', f'{_object_label(ot)}: {name}', r,
                            'it names people, groups or units of this company'
                            if _refers_to_people(r['steps'], r['conditions']) else None))
        return out

    def _split(self, key):
        kind, rest = key.split(':', 1)
        return (kind, rest, None) if kind == 'default' else (kind, *rest.split(':', 1))

    def create(self, target, item):
        kind, ot, name = self._split(item.key)
        if kind == 'default':
            return str(WorkflowDefinition.objects.create(company_id=target.company_id, object_type=ot, **item.values).pk)
        return str(ApprovalFlow.objects.create(company_id=target.company_id, object_type=ot, name=name, **item.values).pk)

    def overwrite(self, target, item):
        kind, ot, name = self._split(item.key)
        if kind == 'default':
            qs = WorkflowDefinition.objects.for_company(target.company_id).filter(object_type=ot)
            qs.update(**item.values)
        else:
            qs = ApprovalFlow.objects.for_company(target.company_id).filter(object_type=ot, name=name)
            qs.update(**item.values, updated_at=Now())
        return str(qs.values_list('pk', flat=True).first())


# ---------------------------------------------------------------- holidays and shifts

class Holidays(Section):
    key, label, table = 'holidays', 'Holidays (from today)', 'holiday'

    def items(self, cid):
        rows = (Holiday.objects.for_company(cid).filter(branch__isnull=True, local_date__gte=npt_today())
                .order_by('local_date', 'name_en').values('local_date', 'bs_date', 'name_en', 'name_ne', 'gender_scope', 'optional'))
        return [Item(f"{r['local_date'].isoformat()}|{r['name_en']}", r['name_en'],
                     {k: v for k, v in r.items() if k not in ('local_date', 'name_en')} | {'date': r['local_date']})
                for r in rows]

    def _vals(self, item):
        return {k: v for k, v in item.values.items() if k != 'date'}

    def create(self, target, item):
        name = item.key.split('|', 1)[1]
        return str(Holiday.objects.create(company_id=target.company_id, local_date=item.values['date'], name_en=name,
                                          **self._vals(item)).pk)

    def overwrite(self, target, item):
        name = item.key.split('|', 1)[1]
        qs = Holiday.objects.for_company(target.company_id).filter(branch__isnull=True, local_date=item.values['date'], name_en=name)
        qs.update(**self._vals(item))
        return str(qs.values_list('pk', flat=True).first())


class Shifts(Section):
    key, label, table = 'shifts', 'Shifts', 'shift'
    FIELDS = ('code', 'name_en', 'name_ne', 'start_time', 'end_time', 'grace_in_min', 'grace_out_min', 'early_in_window_min',
              'late_out_window_min', 'break_minutes', 'half_day_threshold_min', 'absent_threshold_min', 'pairing_mode',
              'rounding_min', 'rounding_direction')

    def items(self, cid):
        rows = Shift.objects.for_company(cid).order_by('code').values(*self.FIELDS)
        return [Item(r['code'], f"{r['name_en']} ({r['code']})", {k: v for k, v in r.items() if k != 'code'}) for r in rows]

    def create(self, target, item):
        return str(Shift.objects.create(company_id=target.company_id, code=item.key, **item.values).pk)

    def overwrite(self, target, item):
        qs = Shift.objects.for_company(target.company_id).filter(code=item.key)
        qs.update(**item.values)
        return str(qs.values_list('pk', flat=True).get())


# ---------------------------------------------------------------- company settings

class Setting(Section):
    table = 'company_setting'

    def __init__(self, setting_key: str, label: str):
        self.setting = setting_key
        self.key = f'setting:{setting_key}'
        self.label = label

    def items(self, cid):
        v = settings_store.get_setting(cid, self.setting)
        return [] if v is None else [Item(self.setting, self.label, {'value': v})]

    def adapt(self, item, target):
        """Late penalties name the leave types they deduct from: the same codes in the target."""
        if self.setting != 'attendance.extras':
            return item
        value = dict(item.values['value'])
        lp = dict(value.get('latePenalty') or {})
        ids = [str(i) for i in lp.get('leaveTypeIds') or []]
        if ids:
            codes = {str(pk): code for pk, code in LeaveType.objects.filter(pk__in=ids).values_list('pk', 'code')}
            lp['leaveTypeIds'] = [target.leave_types[codes[i]] for i in ids if codes.get(i) in target.leave_types]
            value['latePenalty'] = lp
        return Item(item.key, item.label, {'value': value}, item.skip)

    def create(self, target, item):
        # Leave types copied earlier in the same run exist now: remap against them.
        target.leave_types = {c: str(i) for c, i in LeaveType.objects.for_company(target.company_id).values_list('code', 'pk')}
        value = self.adapt(item, target).values['value']
        settings_store.put_setting(target.company_id, self.setting, value)
        return f'{target.company_id}:{self.setting}'

    overwrite = create


SECTIONS: list[Section] = [LeaveTypes(), SalaryComponents(), SalaryStructures(), ApprovalFlows(), Holidays(), Shifts(),
                           *(Setting(k, v) for k, v in SETTING_SECTIONS.items())]
SECTION_BY_KEY = {s.key: s for s in SECTIONS}
SECTION_KEYS = tuple(SECTION_BY_KEY)
SECTION_CHOICES = [(s.key, s.label) for s in SECTIONS]


# ---------------------------------------------------------------- plan, preview, apply

def _target(company) -> Target:
    cid = str(company.pk)
    return Target(cid, company.name_en,
                  set(SalaryComponent.objects.for_company(cid).values_list('code', flat=True)),
                  {c: str(i) for c, i in LeaveType.objects.for_company(cid).values_list('code', 'pk')})


def _plan(actor: AuthUser, body: dict) -> dict:
    scope = group_scope(actor)
    b = plain(validate(PolicyCopyIn, body))
    source = scope.company(b['sourceCompanyId'])
    targets = []
    for t in dict.fromkeys(str(x) for x in b['targetCompanyIds']):
        c = scope.company(t)
        if str(c.pk) == str(source.pk):
            raise BadRequestError('copy to companies other than the source')
        targets.append(c)
    chosen = [s for s in SECTIONS if s.key in b['sections']]
    source_items = {s.key: s.items(str(source.pk)) for s in chosen}
    copy_components = 'salary_components' in b['sections']
    plans = []
    for c in targets:
        tgt = _target(c)
        if copy_components:     # structures may use the components copied in the same run
            tgt.component_codes |= {i.key for i in source_items['salary_components']
                                    if not (i.values.get('statutory') and i.key not in tgt.component_codes)}
        sections = []
        for s in chosen:
            current = s.existing(tgt.company_id)
            rows = []
            for src_item in source_items[s.key]:
                item = s.adapt(Item(src_item.key, src_item.label, dict(src_item.values), src_item.skip), tgt)
                if item.skip:
                    action, reason = 'skip', item.skip
                elif item.key not in current:
                    action, reason = 'create', None
                elif current[item.key] == item.values:
                    action, reason = 'same', None
                elif (why := s.locked(item.key, current[item.key])):
                    action, reason = 'skip', why
                elif b['onConflict'] == 'overwrite':
                    action, reason = 'overwrite', None
                else:
                    action, reason = 'keep', 'exists with different values; kept'
                rows.append({'item': item, 'action': action, 'reason': reason})
            sections.append({'section': s, 'rows': rows})
        plans.append({'target': tgt, 'sections': sections})
    return {'source': source, 'onConflict': b['onConflict'], 'sections': b['sections'], 'plans': plans}


ACTIONS = ('create', 'overwrite', 'keep', 'same', 'skip')


def _shape(plan: dict) -> dict:
    out = []
    for p in plan['plans']:
        secs = []
        for s in p['sections']:
            counts = {a: sum(1 for r in s['rows'] if r['action'] == a) for a in ACTIONS}
            secs.append({'key': s['section'].key, 'label': s['section'].label, 'counts': counts,
                         'items': [{'key': r['item'].key, 'label': r['item'].label, 'action': r['action'],
                                    'reason': r['reason']} for r in s['rows']]})
        totals = {a: sum(s['counts'][a] for s in secs) for a in ACTIONS}
        out.append({'companyId': p['target'].company_id, 'name': p['target'].name, 'sections': secs, 'totals': totals,
                    'summary': f"{totals['create']} to create, {totals['overwrite']} to overwrite, "
                               f"{totals['keep']} kept as they are"})
    return {'source': {'id': str(plan['source'].pk), 'name': plan['source'].name_en}, 'onConflict': plan['onConflict'],
            'targets': out, 'totals': {a: sum(t['totals'][a] for t in out) for a in ACTIONS}}


def preview(actor: AuthUser, body: dict) -> dict:
    """What copying would do, without changing anything."""
    return _shape(_plan(actor, body))


@transaction.atomic
def apply(actor: AuthUser, body: dict) -> dict:
    """Copy as previewed: create what is missing and overwrite conflicts when asked; every row written is audited."""
    plan = _plan(actor, body)
    source_id = str(plan['source'].pk)
    for p in plan['plans']:
        tgt: Target = p['target']
        for s in p['sections']:
            sec: Section = s['section']
            written = []
            for r in s['rows']:
                item = r['item']
                if r['action'] == 'create':
                    row_id = sec.create(tgt, item)
                    audit('create', sec.table, actor_id=actor.id, row_id=row_id,
                          after={'key': item.key, **item.values, 'copiedFrom': source_id})
                elif r['action'] == 'overwrite':
                    before = sec.existing(tgt.company_id).get(item.key)
                    row_id = sec.overwrite(tgt, item)
                    audit('update', sec.table, actor_id=actor.id, row_id=row_id, before=before,
                          after={'key': item.key, **item.values, 'copiedFrom': source_id})
                else:
                    continue
                written.append(item)
            if written:
                sec.after_apply(tgt, written)
        counts = {a: sum(1 for s in p['sections'] for r in s['rows'] if r['action'] == a) for a in ACTIONS}
        audit('copy_policies', 'company', actor_id=actor.id, row_id=tgt.company_id,
              after={'sourceCompanyId': source_id, 'sections': plan['sections'], 'onConflict': plan['onConflict'],
                     'created': counts['create'], 'overwritten': counts['overwrite']})
    return _shape(plan)
