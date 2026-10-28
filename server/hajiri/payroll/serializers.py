"""
The payroll request contract (the zod schemas of payroll/routes.ts, compliance.ts and tax-savings.ts) as DRF
serializers. Field names are the TS body and query keys, so JSON clients, the page forms and the services speak the
same shape; `hajiri.core.validation.validate` reports problems in the TS issue list.
"""
from __future__ import annotations

from urllib.parse import urlsplit
import re

from rest_framework import serializers
from rest_framework.fields import empty

from hajiri.core.validation import IsoDateField, MoneyField
from hajiri.db.models import PayrollHold, PayrollOpeningBalance, PayrollRun, SalaryComponent

CODE_RE = r'^[A-Z][A-Z0-9_]{1,30}$'
RUN_TYPES = tuple(t for t in PayrollRun.RunType.values if t != PayrollRun.RunType.BONUS)   # bonus runs have their own route
COMPONENT_TYPES = tuple(SalaryComponent.Type.values)
ROUNDINGS = tuple(SalaryComponent.Rounding.values)
HOLD_FILTERS = (*PayrollHold.Status.values, 'open')
RELEASE_MODES = tuple(PayrollHold.ReleaseMode.values)
OPENING_SOURCES = tuple(PayrollOpeningBalance.Source.values)
DECL_FIELDS = ('lifePremium', 'healthPremium', 'citMonthly', 'medicalCost', 'otherRetirementAnnual', 'donation')
OPENING_REFINE = 'taxable income cannot be more than gross plus the employer contribution'


# ---------------------------------------------------------------- field helpers (the zod field shapes)

def text(*, min: int | None = None, max: int | None = None, trim: bool = False, required: bool = True, null: bool = False,
         default=empty, regex: str | None = None, regex_message: str = 'Invalid string') -> serializers.CharField:
    """A string field: `min`/`max` length, `trim` like zod .trim(), `null` for nullish, `regex` with its message."""
    kw: dict = {'trim_whitespace': trim, 'required': required and default is empty, 'allow_null': null}
    if default is not empty:
        kw['default'] = default
    if max is not None:
        kw['max_length'] = max
    messages = {}
    if min:
        kw['min_length'] = min
        messages['blank'] = f'Too small: expected string to have >={min} characters'
    else:
        kw['allow_blank'] = True
    if regex:
        messages['invalid'] = regex_message
        return serializers.RegexField(regex, error_messages=messages, **kw)
    return serializers.CharField(error_messages=messages, **kw)


def _not_negative(v: str) -> None:
    if isinstance(v, str) and v.startswith('-'):
        raise serializers.ValidationError('must not be negative')


def amount(**kw) -> MoneyField:
    """A money amount as a string with up to 2 decimals that must not be negative."""
    return MoneyField(validators=[_not_negative], trim_whitespace=False, **kw)


def money(**kw) -> MoneyField:
    """A money amount that may be negative (notice pay deducted)."""
    return MoneyField(trim_whitespace=False, **kw)


def fiscal_year(**kw) -> serializers.IntegerField:
    return serializers.IntegerField(min_value=2070, max_value=2200, **kw)


def bs_month(**kw) -> serializers.IntegerField:
    return serializers.IntegerField(min_value=1, max_value=12, **kw)


def uuid_list(**kw) -> serializers.ListField:
    return serializers.ListField(child=serializers.UUIDField(), **kw)


def is_url(v: str) -> bool:
    """A parseable absolute URL: a scheme and, for http(s), a host."""
    try:
        u = urlsplit(v)
    except ValueError:
        return False
    if not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*$', u.scheme or '') or any(c.isspace() for c in v):
        return False
    return bool(u.netloc) if u.scheme.lower() in ('http', 'https') else True


# ---------------------------------------------------------------- rules, components, structures, assignments

class RulesQueryIn(serializers.Serializer):
    key = text(required=False)
    asOf = IsoDateField(required=False)


class RuleIn(serializers.Serializer):
    ruleKey = text(min=1, max=60)
    validFromAd = IsoDateField()
    validToAd = IsoDateField(required=False, allow_null=True)
    validFromBs = text()
    validToBs = text(required=False, null=True)
    value = serializers.JSONField(required=False)
    sourceUrl = text(required=False, regex=r'^https?://', regex_message='source must be an http(s) link')
    note = text(max=500, required=False)
    supersedesId = serializers.UUIDField(required=False)

    def validate_sourceUrl(self, v):
        if not is_url(v):
            raise serializers.ValidationError('Invalid URL')
        return v


class ComponentIn(serializers.Serializer):
    code = text(regex=CODE_RE)
    nameEn = text(min=1)
    nameNe = text(required=False)
    type = serializers.ChoiceField(COMPONENT_TYPES)
    formula = text(max=500, required=False, null=True)
    condition = text(max=500, required=False, null=True)
    dependsOnPaymentDays = serializers.BooleanField(default=False)
    taxable = serializers.BooleanField(default=True)
    rounding = serializers.ChoiceField(ROUNDINGS, default=SalaryComponent.Rounding.PAISA)
    sortOrder = serializers.IntegerField(default=100)
    active = serializers.BooleanField(default=True)


class ComponentPatchIn(ComponentIn):
    """Every key may be left out; the defaults still fill missing keys (the TS route's zod .partial())."""
    code = text(regex=CODE_RE, required=False)
    nameEn = text(min=1, required=False)
    type = serializers.ChoiceField(COMPONENT_TYPES, required=False)


class ComponentRefIn(serializers.Serializer):
    code = text()


def component_list():
    return ComponentRefIn(many=True, allow_empty=False)


class StructureIn(serializers.Serializer):
    code = text(min=1, max=20)
    nameEn = text(min=1)
    components = component_list()


class StructureVersionIn(serializers.Serializer):
    components = component_list()


class EmployeeQueryIn(serializers.Serializer):
    employeeId = serializers.UUIDField(required=False)


class AssignmentIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    structureVersionId = serializers.UUIDField()
    # `from` is a Python keyword: declared in __init__ below.
    baseAmounts = serializers.DictField(child=amount())

    def get_fields(self):
        fields = super().get_fields()
        fields['from'] = IsoDateField()
        return fields


# ---------------------------------------------------------------- declarations and loans

class DeclarationAmountsIn(serializers.Serializer):
    lifePremium = amount(default='0')
    healthPremium = amount(default='0')
    citMonthly = amount(default='0')
    medicalCost = amount(default='0')
    otherRetirementAnnual = amount(default='0')
    donation = amount(default='0')


class DeclarationQueryIn(serializers.Serializer):
    employeeId = serializers.UUIDField(required=False)
    fiscalYearStart = serializers.IntegerField()


class DeclarationIn(DeclarationAmountsIn):
    employeeId = serializers.UUIDField(required=False)
    fiscalYearStart = serializers.IntegerField()
    submit = serializers.BooleanField(default=True)


class DeclarationVerifyIn(serializers.Serializer):
    fiscalYearStart = serializers.IntegerField()
    decision = serializers.ChoiceField(('verified', 'rejected'))
    reason = text(trim=True, max=500, required=False)


class LoanIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    kind = serializers.ChoiceField(('loan', 'advance'), default='loan')
    principal = money()
    emi = money()
    startBsYear = serializers.IntegerField()
    startBsMonth = bs_month()
    note = text(required=False)


# ---------------------------------------------------------------- periods and runs

class PeriodIn(serializers.Serializer):
    bsYear = serializers.IntegerField()
    bsMonth = bs_month()


class RunIn(serializers.Serializer):
    periodId = serializers.UUIDField()
    runType = serializers.ChoiceField(RUN_TYPES, default=PayrollRun.RunType.REGULAR)
    referencesRunId = serializers.UUIDField(required=False)
    employeeIds = uuid_list(required=False)
    noticePay = money(required=False)
    notes = text(max=1000, required=False)


class BonusRunIn(serializers.Serializer):
    periodId = serializers.UUIDField()
    fiscalYearStart = serializers.IntegerField()
    netProfit = amount()


class InputLineIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    componentCode = text(regex=CODE_RE)
    amount = money()
    note = text(max=200, required=False)


class InputsIn(serializers.Serializer):
    inputs = InputLineIn(many=True, allow_empty=False, max_length=5000)


class ComputeIn(serializers.Serializer):
    overrideReason = text(min=5, max=500, required=False)


class ReviewIn(serializers.Serializer):
    notes = text(max=2000, required=False)


class SheetQueryIn(serializers.Serializer):
    format = serializers.ChoiceField(('json', 'csv', 'html'), default='json')


class PayslipsQueryIn(serializers.Serializer):
    employeeId = serializers.UUIDField(required=False)
    fiscalYearStart = serializers.IntegerField(required=False)


# ---------------------------------------------------------------- compliance

class OpeningsQueryIn(serializers.Serializer):
    fiscalYearStart = fiscal_year(required=False)


class OpeningIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    fiscalYearStart = fiscal_year()
    gross = amount(default='0')
    taxable = amount(default='0')
    tdsPaid = amount(default='0')
    retirementEmployee = amount(default='0')
    retirementEmployer = amount(default='0')
    festivalPaid = serializers.BooleanField(default=False)
    months = serializers.IntegerField(min_value=0, max_value=12, default=0)
    source = serializers.ChoiceField(OPENING_SOURCES, default=PayrollOpeningBalance.Source.PREVIOUS_EMPLOYER)
    note = text(trim=True, max=500, required=False, null=True)

    def validate(self, attrs):
        from decimal import localcontext

        from engines.payroll.numeric import ENGINE_CONTEXT, D
        with localcontext(ENGINE_CONTEXT):
            if not D(attrs['taxable']) <= D(attrs['gross']) + D(attrs['retirementEmployer']):
                raise serializers.ValidationError({'taxable': OPENING_REFINE})
        return attrs


class ApplyCitIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    monthlyCit = amount()
    fromBsMonth = bs_month(required=False)


class HoldsQueryIn(serializers.Serializer):
    status = serializers.ChoiceField(HOLD_FILTERS, default='open')


class HoldIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    reason = text(trim=True, min=3, max=500)


class ReleaseIn(serializers.Serializer):
    mode = serializers.ChoiceField(RELEASE_MODES)
    note = text(trim=True, max=500, required=False)


class CertificateQueryIn(serializers.Serializer):
    fiscalYearStart = fiscal_year(required=False)
    kind = serializers.ChoiceField(('tax', 'salary'), default='tax')
    purpose = text(trim=True, max=120, required=False)


def _format():
    return serializers.ChoiceField(('json', 'csv'), default='json')


class AnnualQueryIn(serializers.Serializer):
    fiscalYearStart = fiscal_year(required=False)
    format = _format()


class MonthQueryIn(serializers.Serializer):
    bsYear = fiscal_year()
    bsMonth = bs_month()
    format = _format()


class VarianceQueryIn(serializers.Serializer):
    runId = serializers.UUIDField()
    format = _format()


class LiabilityQueryIn(serializers.Serializer):
    asOf = IsoDateField(required=False)
    format = _format()


# ---------------------------------------------------------------- tax savings

class SavingsQueryIn(serializers.Serializer):
    fiscalYearStart = fiscal_year(required=False)
    employeeId = serializers.UUIDField(required=False)


class SavingsIn(DeclarationAmountsIn):
    fiscalYearStart = fiscal_year()
    note = text(trim=True, max=1000, required=False)
    proofFileIds = uuid_list(max_length=10, default=list)


class EstimateIn(serializers.Serializer):
    fiscalYearStart = fiscal_year(required=False)
    employeeId = serializers.UUIDField(required=False)
    proposed = DeclarationAmountsIn(required=False)
