"""
Per-company defaults (port of apps/api/src/db/seed-defaults.ts): statutory leave types, approval workflows and
company settings, then every module's own defaults registered with `company_seeder` (payroll rule versions and salary
components, expense categories, document categories and letter templates). Idempotent: running it again adds only
what is missing and never changes what HR edited.
"""
from __future__ import annotations

import json

from django.db import transaction

from hajiri.core.db import execute, one
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


@transaction.atomic
def seed_defaults(company_id: str) -> dict[str, int]:
    n = 0
    for i, t in enumerate(LEAVE_TYPES):
        r = one("""INSERT INTO leave_type (company_id, code, name_en, name_ne, paid, statutory, accrual_basis, accrual_days,
                     accrual_per_worked_days, max_balance, carry_forward, encashable, certificate_after_days, allow_half_day,
                     gender_scope, sandwich_rule, expiry_days, sort_order)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (company_id, code) DO NOTHING RETURNING id""", [company_id, *t, i])
        # Home and sick leave can be taken by the hour from the start (HR can switch it off; re-seeding never turns it back on).
        if r and t[0] in ('HOME', 'SICK'):
            execute('UPDATE leave_type SET allow_hourly = true WHERE id = %s', [r['id']])
        n += 1 if r else 0
    # Event-based statutory leave: paid days per event (maternity 60 of 98, maternity-care 15, mourning 13), not a balance.
    execute("""UPDATE leave_type SET event_days = v.days FROM (VALUES ('MATERNITY', 60), ('PATERNITY', 15), ('MOURNING', 13)) AS v(code, days)
                WHERE leave_type.company_id = %s AND leave_type.code = v.code AND leave_type.event_days IS NULL""", [company_id])
    w = 0
    for object_type, steps in WORKFLOWS:
        w += execute("""INSERT INTO workflow_definition (company_id, object_type, steps) VALUES (%s, %s, %s)
                        ON CONFLICT (company_id, object_type) DO NOTHING""", [company_id, object_type, json.dumps(steps)])
    s = 0
    for key, value in SETTINGS:
        s += execute('INSERT INTO company_setting (company_id, key, value) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING',
                     [company_id, key, json.dumps(value)])
    out = {'leaveTypes': n, 'workflows': w, 'settings': s}
    for seeder in seeders():
        out.update(seeder.fn(str(company_id)) or {})
    return out


@transaction.atomic
def create_company(code: str, name_en: str, name_ne: str | None = None, *, seed: bool = True) -> dict:
    """A new company with its defaults seeded, as `create-company` in the CLI."""
    c = one('INSERT INTO company (code, name_en, name_ne) VALUES (%s, %s, %s) RETURNING id, code, name_en, name_ne',
            [code.strip(), name_en.strip(), name_ne])
    c['id'] = str(c['id'])
    c['seeded'] = seed_defaults(c['id']) if seed else {}
    return c
