"""Company settings and their schemas (port of apps/api/src/modules/settings/schemas.ts)."""
from __future__ import annotations

import hashlib
import json

from hajiri.core.schema import Arr, Bool, Enum, Hhmm, Int, Num, Obj, Str

# Statutory maxima (Labour Act 2074 Sec 30(1)) can be lowered by policy, never raised.
OT_CAP_DAY_MIN = 240
OT_CAP_WEEK_MIN = 1440

_LATE_PENALTY_DEFAULT = {'enabled': False, 'lateCount': 3, 'deductionDays': 0.5}

SETTING_SCHEMAS: dict[str, Obj] = {
    'attendance.policy': Obj({
        'version': Str(optional=True),
        'otMinThresholdMin': Int(min=0, max=240, default=30),
        'otRoundingMin': Int(min=0, max=60, default=15),
        'requirePreapproval': Bool(default=True),
        'otCapDayMin': Int(min=0, default=OT_CAP_DAY_MIN, transform=lambda v: min(v, OT_CAP_DAY_MIN)),
        'otCapWeekMin': Int(min=0, default=OT_CAP_WEEK_MIN, transform=lambda v: min(v, OT_CAP_WEEK_MIN)),
        'holidayWork': Enum(('substitute_leave', 'pay', 'employee_choice'), default='substitute_leave'),
        'nightWindowEnabled': Bool(default=False),
        'nightPremiumRate': Num(min=0, max=1, default=0),
        'autoClockOutAfterMin': Int(min=0, max=720, default=0),
        'regularisationBackdateDays': Int(min=0, max=90, default=30),
        'regularisationMonthlyCap': Int(min=0, max=31, default=5),
        'wfhPhotoRequired': Bool(default=True),
        'wfhPhotoRetentionDays': Int(min=7, max=365, default=90),
        'latePenalty': Obj({'enabled': Bool(default=False), 'lateCount': Int(min=1, default=3),
                            'deductionDays': Num(min=0, max=1, default=0.5)}, default=lambda: dict(_LATE_PENALTY_DEFAULT)),
    }),
    'leave.policy': Obj({
        'leaveYear': Enum(('fiscal', 'anniversary'), default='fiscal'),
        'sandwichRule': Bool(default=False),
        'probationRestriction': Bool(default=True),
        'maxContinuousDays': Int(min=1, max=365, default=30),
    }),
    'payroll.policy': Obj({
        'prorationBasis': Enum(('bs_calendar_days', 'fixed_30', '26_day'), default='bs_calendar_days'),
        'hourlyDivisor': Int(min=150, max=260, default=208),
        'payDay': Int(min=1, max=32, default=25),
        'rounding': Enum(('half_up_rupee', 'paisa'), default='half_up_rupee'),
        'separateApprover': Bool(default=False),
        'encashmentDivisor': Enum(('bs_days', 'fixed_30'), default='bs_days'),
        'publishOnLock': Bool(default=False),
    }),
    'payslip.policy': Obj({
        'language': Enum(('both', 'en', 'ne'), default='both'),
        'numerals': Enum(('latin', 'devanagari'), default='latin'),
        'passwordProtect': Bool(default=True),
    }),
    'notifications.policy': Obj({
        'quietHoursStart': Hhmm(default='21:00'),
        'quietHoursEnd': Hhmm(default='07:00'),
        'slaHours': Int(min=1, max=168, default=24),
        'escalationHours': Int(min=1, max=336, default=48),
        'digestTime': Hhmm(default='10:45'),
    }),
    'company.profile': Obj({
        'nameEn': Str(min=1, max=120),
        'nameNe': Str(max=120, nullish=True),
        'address': Str(max=300, nullish=True),
        'pan': Str(regex=r'^\d{9}$', nullish=True),
        'ssfEmployerId': Str(max=20, nullish=True),
        'logoDataUrl': Str(max=200_000, regex=r'^data:image/(png|jpeg|svg\+xml);base64,', nullish=True),
    }),
    'security.policy': Obj({
        # Roles that must use two-step sign-in; they are sent to set it up at their next sign-in.
        'require2faRoles': Arr(Enum(('admin', 'hr', 'manager', 'employee')), max=4, default=list,
                               transform=lambda a: list(dict.fromkeys(a))),
    }),
}

SETTING_KEYS: tuple[str, ...] = tuple(SETTING_SCHEMAS)

SETTING_DESCRIPTIONS = {
    'attendance.policy': 'Overtime threshold/rounding/approval, holiday work, night premium, auto clock-out, regularisation limits. Statutory OT caps (4 h/day, 24 h/week) cannot be raised.',
    'leave.policy': 'Leave year, sandwich rule, probation restriction, maximum continuous days.',
    'payroll.policy': 'Proration basis, hourly divisor, pay day, rounding, whether a different HR user must approve, encashment divisor, whether payslips are published on lock.',
    'payslip.policy': 'Payslip language, numerals and PDF protection.',
    'notifications.policy': 'Quiet hours, SLA and escalation timing, digest time.',
    'company.profile': 'Company name, address, PAN, SSF employer ID and logo shown on payslips and the app header.',
    'security.policy': 'Roles that must use two-step sign-in (an authenticator app code after the password).',
}


def _js_number(v):
    """A number as JSON.stringify writes it (1.0 -> 1)."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _js_stringify_top_sorted(value: dict) -> str:
    """JSON.stringify(rest, Object.keys(rest).sort()): the replacer array is an allow-list applied at every depth, so
    nested objects keep only keys that are also top-level keys, written in the sorted order."""
    allow = sorted(value)

    def enc(v):
        if isinstance(v, dict):
            return '{' + ','.join(f'{json.dumps(k, ensure_ascii=False)}:{enc(v[k])}' for k in allow if k in v) + '}'
        if isinstance(v, list):
            return '[' + ','.join(enc(x) for x in v) + ']'
        if isinstance(v, bool) or v is None:
            return json.dumps(v)
        if isinstance(v, (int, float)):
            return json.dumps(_js_number(v))
        return json.dumps(v, ensure_ascii=False)
    return enc(value)


def policy_version(value: dict) -> str:
    """Deterministic version tag so a recompute notices a policy edit (inputs_hash includes the policy version)."""
    rest = {k: v for k, v in value.items() if k != 'version'}
    return 'v-' + hashlib.sha256(_js_stringify_top_sorted(rest).encode('utf-8')).hexdigest()[:12]
