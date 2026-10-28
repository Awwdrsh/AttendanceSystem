"""Company settings and their request contracts (port of apps/api/src/modules/settings/schemas.ts): one DRF serializer
per setting key, with the TS defaults, bounds and messages. Validating `{}` gives a setting's defaults."""
from __future__ import annotations

import hashlib
import json

from rest_framework import serializers

from hajiri.core.validation import HhmmField

from .fields import JsonBool, JsonNumber, JsonString

# Statutory maxima (Labour Act 2074 Sec 30(1)) can be lowered by policy, never raised.
OT_CAP_DAY_MIN = 240
OT_CAP_WEEK_MIN = 1440

_LATE_PENALTY_DEFAULT = {'enabled': False, 'lateCount': 3, 'deductionDays': 0.5}


def _int(**kw) -> JsonNumber:
    return JsonNumber(integer=True, **kw)


class LatePenaltyIn(serializers.Serializer):
    enabled = JsonBool(default=False)
    lateCount = _int(min_value=1, default=3)
    deductionDays = JsonNumber(min_value=0, max_value=1, default=0.5)


class AttendancePolicyIn(serializers.Serializer):
    version = JsonString(required=False)
    otMinThresholdMin = _int(min_value=0, max_value=240, default=30)
    otRoundingMin = _int(min_value=0, max_value=60, default=15)
    requirePreapproval = JsonBool(default=True)
    otCapDayMin = _int(min_value=0, clamp_max=OT_CAP_DAY_MIN, default=OT_CAP_DAY_MIN)
    otCapWeekMin = _int(min_value=0, clamp_max=OT_CAP_WEEK_MIN, default=OT_CAP_WEEK_MIN)
    holidayWork = serializers.ChoiceField(('substitute_leave', 'pay', 'employee_choice'), default='substitute_leave')
    nightWindowEnabled = JsonBool(default=False)
    nightPremiumRate = JsonNumber(min_value=0, max_value=1, default=0)
    autoClockOutAfterMin = _int(min_value=0, max_value=720, default=0)
    regularisationBackdateDays = _int(min_value=0, max_value=90, default=30)
    regularisationMonthlyCap = _int(min_value=0, max_value=31, default=5)
    wfhPhotoRequired = JsonBool(default=True)
    wfhPhotoRetentionDays = _int(min_value=7, max_value=365, default=90)
    latePenalty = LatePenaltyIn(default=lambda: dict(_LATE_PENALTY_DEFAULT))


class LeavePolicyIn(serializers.Serializer):
    leaveYear = serializers.ChoiceField(('fiscal', 'anniversary'), default='fiscal')
    sandwichRule = JsonBool(default=False)
    probationRestriction = JsonBool(default=True)
    maxContinuousDays = _int(min_value=1, max_value=365, default=30)


class PayrollPolicyIn(serializers.Serializer):
    prorationBasis = serializers.ChoiceField(('bs_calendar_days', 'fixed_30', '26_day'), default='bs_calendar_days')
    hourlyDivisor = _int(min_value=150, max_value=260, default=208)
    payDay = _int(min_value=1, max_value=32, default=25)
    rounding = serializers.ChoiceField(('half_up_rupee', 'paisa'), default='half_up_rupee')
    separateApprover = JsonBool(default=False)
    encashmentDivisor = serializers.ChoiceField(('bs_days', 'fixed_30'), default='bs_days')
    publishOnLock = JsonBool(default=False)


class PayslipPolicyIn(serializers.Serializer):
    language = serializers.ChoiceField(('both', 'en', 'ne'), default='both')
    numerals = serializers.ChoiceField(('latin', 'devanagari'), default='latin')
    passwordProtect = JsonBool(default=True)


class NotificationsPolicyIn(serializers.Serializer):
    quietHoursStart = HhmmField(default='21:00')
    quietHoursEnd = HhmmField(default='07:00')
    slaHours = _int(min_value=1, max_value=168, default=24)
    escalationHours = _int(min_value=1, max_value=336, default=48)
    digestTime = HhmmField(default='10:45')


def _nullish(max_length: int, **kw) -> JsonString:
    return JsonString(max_length=max_length, required=False, allow_null=True, **kw)


class CompanyProfileIn(serializers.Serializer):
    nameEn = JsonString(min_length=1, max_length=120, allow_blank=False)
    nameNe = _nullish(120)
    address = _nullish(300)
    pan = JsonString(pattern=r'^\d{9}$', required=False, allow_null=True)
    ssfEmployerId = _nullish(20)
    logoDataUrl = _nullish(200_000, pattern=r'^data:image/(png|jpeg|svg\+xml);base64,')


class SecurityPolicyIn(serializers.Serializer):
    # Roles that must use two-step sign-in; they are sent to set it up at their next sign-in.
    require2faRoles = serializers.ListField(child=serializers.ChoiceField(('admin', 'hr', 'manager', 'employee')),
                                            max_length=4, default=list)

    def validate_require2faRoles(self, value):
        return list(dict.fromkeys(value))


SETTING_SCHEMAS: dict[str, type[serializers.Serializer]] = {
    'attendance.policy': AttendancePolicyIn,
    'leave.policy': LeavePolicyIn,
    'payroll.policy': PayrollPolicyIn,
    'payslip.policy': PayslipPolicyIn,
    'notifications.policy': NotificationsPolicyIn,
    'company.profile': CompanyProfileIn,
    'security.policy': SecurityPolicyIn,
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
    """JSON.stringify(rest, Object.keys(rest).sort()), byte for byte: the policy version must match what the TS stack
    computes for the same settings (attendance_day.inputs_hash includes it, and both stacks share the database). The
    replacer array is an allow-list applied at every depth, so nested objects keep only keys that are also top-level
    keys, written in the sorted order."""
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
