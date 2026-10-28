"""
Per-company defaults (port of apps/api/src/db/seed-defaults.ts): statutory leave types, approval workflows and
company settings, then every module's own defaults registered with `company_seeder` (payroll rule versions and salary
components, expense categories, document categories and letter templates). Idempotent: running it again adds only
what is missing and never changes what HR edited.
"""
from __future__ import annotations

from django.db import transaction

from hajiri.core.settings_store import add_setting_if_missing
from hajiri.db.models import Company, LeaveType, WorkflowDefinition
from hajiri.worker.registry import seeders

# code, name_en, name_ne, paid, statutory, basis, accrual_days, per_worked_days, max_balance, carry, encashable,
# cert_after, half_day, gender, sandwich, expiry
LEAVE_TYPES = (
    ('HOME', 'Home leave', 'घर बिदा', True, True, 'worked_days', 1, 20, 90, True, True, None, True, 'all', False, None),
    ('SICK', 'Sick leave', 'बिरामी बिदा', True, True, 'monthly', 1, None, 45, True, True, 3, True, 'all', False, None),
    ('MATERNITY', 'Maternity leave', 'प्रसूति बिदा', True, True, 'none', 0, None, None, False, False, None, False, 'female', False, None),
    ('PATERNITY', 'Maternity-care leave', 'प्रसूति स्याहार बिदा', True, True, 'none', 0, None, None, False, False, None, False, 'male', False, None),
    ('MOURNING', 'Mourning leave', 'किरिया बिदा', True, True, 'none', 0, None, None, False, False, None, False, 'all', False, None),
    ('SUBSTITUTE', 'Substitute leave', 'सट्टा बिदा', True, True, 'none', 0, None, None, False, False, None, True, 'all', False, 21),
    ('CASUAL', 'Casual leave', 'भैपरी बिदा', True, False, 'annual', 6, None, 6, False, False, None, True, 'all', False, None),
    ('LWP', 'Leave without pay', 'बेतलबी बिदा', False, False, 'none', 0, None, None, False, False, None, True, 'all', True, None),
    ('SPECIAL_UNPAID', 'Special unpaid leave', 'विशेष बेतलबी बिदा', False, True, 'none', 0, None, None, False, False, None, False, 'all', False, None),
)

WORKFLOWS = (
    ('leave_request', [{'role': 'manager'}]),
    ('regularisation_request', [{'role': 'manager'}]),
    ('ot_request', [{'role': 'manager'}]),
    ('payroll_run', [{'role': 'hr'}]),
    ('timesheet', [{'role': 'manager'}]),
    ('shift_swap', [{'role': 'manager'}]),
    ('letter_request', [{'role': 'hr'}]),
    ('probation_review', [{'role': 'hr'}]),
    ('resignation', [{'role': 'manager'}, {'role': 'hr'}]),
    ('job_requisition', [{'role': 'hr'}]),
    ('job_offer', [{'role': 'hr'}]),
    ('profile_change', [{'role': 'hr'}]),
    ('training_nomination', [{'role': 'manager'}]),
    ('tax_declaration', [{'role': 'hr'}]),
)

SETTINGS = (
    ('attendance.policy', {'version': 'default-v1', 'otMinThresholdMin': 30, 'otRoundingMin': 15, 'requirePreapproval': True,
                           'holidayWork': 'substitute_leave', 'nightWindowEnabled': False, 'nightPremiumRate': 0,
                           'autoClockOutAfterMin': 0, 'regularisationBackdateDays': 30, 'regularisationMonthlyCap': 5,
                           'wfhPhotoRequired': True, 'wfhPhotoRetentionDays': 90}),
    ('leave.policy', {'leaveYear': 'fiscal', 'sandwichRule': False, 'probationRestriction': True, 'maxContinuousDays': 30}),
    ('payroll.policy', {'prorationBasis': 'bs_calendar_days', 'hourlyDivisor': 208, 'payDay': 25, 'rounding': 'half_up_rupee',
                        'separateApprover': False, 'encashmentDivisor': 'bs_days'}),
    ('payslip.policy', {'language': 'both', 'numerals': 'latin', 'passwordProtect': True}),
    ('time.policy', {'enabled': True, 'requireProject': False, 'roundingStep': 0, 'roundingMode': 'nearest',
                     'fridayReminder': True, 'autoStopHours': 12}),
    ('hiring.policy', {'retentionDays': 180, 'careersEnabled': True}),
    ('people.policy', {'makerChecker': False}),
    ('attendance.extras', {'blockLateClockIn': False, 'blockAfterMin': 0, 'whosOutScope': 'team',
                           'latePenalty': {'enabled': False, 'lateCount': 3, 'minLateMinutes': 1, 'penaltyDays': 0.5,
                                           'leaveTypeIds': []}}),
    ('notifications.policy', {'quietHoursStart': '21:00', 'quietHoursEnd': '07:00', 'slaHours': 24, 'escalationHours': 48}),
)


_LEAVE_FIELDS = ('code', 'name_en', 'name_ne', 'paid', 'statutory', 'accrual_basis', 'accrual_days', 'accrual_per_worked_days',
                 'max_balance', 'carry_forward', 'encashable', 'certificate_after_days', 'allow_half_day', 'gender_scope',
                 'sandwich_rule', 'expiry_days')
# Event-based statutory leave: paid days per event (maternity 60 of 98, maternity-care 15, mourning 13), not a balance.
EVENT_DAYS = {'MATERNITY': 60, 'PATERNITY': 15, 'MOURNING': 13}
# Home and sick leave can be taken by the hour from the start (HR can switch it off; re-seeding never turns it back on).
HOURLY = ('HOME', 'SICK')


@transaction.atomic
def seed_defaults(company_id: str) -> dict[str, int]:
    n = 0
    for i, t in enumerate(LEAVE_TYPES):
        row = dict(zip(_LEAVE_FIELDS, t))
        _, created = LeaveType.objects.get_or_create(
            company_id=company_id, code=row.pop('code'),
            defaults={**row, 'sort_order': i, 'allow_hourly': t[0] in HOURLY})
        n += created
    for code, days in EVENT_DAYS.items():
        LeaveType.objects.for_company(company_id).filter(code=code, event_days__isnull=True).update(event_days=days)
    w = sum(WorkflowDefinition.objects.get_or_create(company_id=company_id, object_type=object_type,
                                                     defaults={'steps': steps})[1]
            for object_type, steps in WORKFLOWS)
    s = sum(add_setting_if_missing(company_id, key, value) for key, value in SETTINGS)
    out = {'leaveTypes': n, 'workflows': w, 'settings': s}
    for seeder in seeders():
        out.update(seeder.fn(str(company_id)) or {})
    return out


@transaction.atomic
def create_company(code: str, name_en: str, name_ne: str | None = None, *, seed: bool = True) -> dict:
    """A new company with its defaults seeded, as `create-company` in the CLI."""
    c = Company.objects.create(code=code.strip(), name_en=name_en.strip(), name_ne=name_ne)
    out = {'id': str(c.pk), 'code': c.code, 'name_en': c.name_en, 'name_ne': c.name_ne}
    out['seeded'] = seed_defaults(out['id']) if seed else {}
    return out
