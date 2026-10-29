"""
The expenses request contract (the zod schemas of apps/api/src/modules/expenses/routes.ts) as DRF serializers.
Field names are the TS body keys, so JSON clients, the page forms and the services speak one shape;
`hajiri.core.validation.validate` reports problems in the TS issue list.
"""
from __future__ import annotations

from rest_framework import serializers

from hajiri.core.validation import IsoDateField
from hajiri.db.models import ExpenseCategory, ExpenseClaim, TravelBill, TravelRequest

DESTINATIONS = tuple(TravelRequest.DestinationCategory.values)
MODES = tuple(TravelRequest.Mode.values)
# Travel bills are transport, lodging or other on the forms (the table also allows food, never offered).
BILL_KINDS = (TravelBill.Kind.TRANSPORT, TravelBill.Kind.LODGING, TravelBill.Kind.OTHER)
CLAIM_STATUSES = tuple(ExpenseClaim.Status.values)
TRIP_STATUSES = tuple(TravelRequest.Status.values)
SCOPES = ('own', 'team', 'all')
DECISIONS = ('approve', 'reject')
GROUP_BY = ('category', 'department', 'month', 'employee')


def amount(**kw) -> serializers.RegexField:
    """A money string with up to 2 decimals (zod regex, no trimming)."""
    return serializers.RegexField(r'^\d{1,10}(\.\d{1,2})?$', trim_whitespace=False,
                                  error_messages={'invalid': 'amount with up to 2 decimals'}, **kw)


def fx_rate(**kw) -> serializers.RegexField:
    return serializers.RegexField(r'^\d{1,6}(\.\d{1,4})?$', trim_whitespace=False,
                                  error_messages={'invalid': 'exchange rate with up to 4 decimals'}, **kw)


def _nullish(field_factory, **kw):
    return field_factory(required=False, allow_null=True, **kw)


class _Km(serializers.FloatField):
    """km travelled: 0 < km <= 5000 (z.number().min(0).max(5000).refine(v > 0))."""

    def __init__(self, **kw):
        super().__init__(min_value=0, max_value=5000, **kw)

    def to_internal_value(self, data):
        v = super().to_internal_value(data)
        if not v > 0:
            raise serializers.ValidationError('Too small: expected number to be >0', code='custom')
        return v


# ---------------------------------------------------------------- policy

class SettingsIn(serializers.Serializer):
    mileageRatePerKm = amount()
    limitMode = serializers.ChoiceField(('block', 'warn'))
    claimWindowDays = serializers.IntegerField(min_value=7, max_value=365)


class CategoryIn(serializers.Serializer):
    code = serializers.RegexField(r'^[A-Z][A-Z0-9_]{1,29}$', trim_whitespace=False,
                                  error_messages={'invalid': 'capital letters, digits and _'})
    name = serializers.CharField(min_length=1, max_length=80)
    kind = serializers.ChoiceField(ExpenseCategory.Kind.values)
    receiptRequiredAbove = _nullish(amount)
    active = serializers.BooleanField(required=False)
    sortOrder = serializers.IntegerField(min_value=0, max_value=9999, required=False)


class CategoryPatchIn(serializers.Serializer):
    """Only the keys sent are changed (receiptRequiredAbove: null clears the threshold)."""
    name = serializers.CharField(min_length=1, max_length=80, required=False)
    kind = serializers.ChoiceField(ExpenseCategory.Kind.values, required=False)
    receiptRequiredAbove = _nullish(amount)
    active = serializers.BooleanField(required=False)
    sortOrder = serializers.IntegerField(min_value=0, max_value=9999, required=False)


class LimitIn(serializers.Serializer):
    grade = serializers.CharField(min_length=1, max_length=40)
    perClaim = _nullish(amount)
    perMonth = _nullish(amount)


class LimitsIn(serializers.Serializer):
    limits = serializers.ListField(child=LimitIn(), max_length=50)


class TadaRateIn(serializers.Serializer):
    grade = serializers.CharField(min_length=1, max_length=40)
    destination = serializers.ChoiceField(DESTINATIONS)
    daRate = amount()
    lodgingRate = amount()


class TadaRatesIn(serializers.Serializer):
    rates = serializers.ListField(child=TadaRateIn(), max_length=200)


# ---------------------------------------------------------------- claims

class ItemIn(serializers.Serializer):
    expenseDate = IsoDateField()
    categoryId = serializers.UUIDField()
    description = serializers.CharField(min_length=1, max_length=300)
    amount = amount(required=False)
    km = _Km(required=False)
    receiptFileId = serializers.UUIDField(required=False, allow_null=True)


def _items():
    return serializers.ListField(child=ItemIn(), min_length=1, max_length=50)


class ClaimIn(serializers.Serializer):
    title = serializers.CharField(min_length=1, max_length=120)
    items = _items()
    submit = serializers.BooleanField(required=False)


class CheckIn(serializers.Serializer):
    items = _items()
    claimId = serializers.UUIDField(required=False)


class ClaimsQueryIn(serializers.Serializer):
    scope = serializers.ChoiceField(SCOPES, required=False)
    status = serializers.ChoiceField(CLAIM_STATUSES, required=False)
    employeeId = serializers.UUIDField(required=False)


class LineReviewIn(serializers.Serializer):
    id = serializers.UUIDField()
    approvedAmount = amount()
    reason = serializers.CharField(max_length=300, required=False, allow_blank=True)


class ClaimReviewIn(serializers.Serializer):
    decision = serializers.ChoiceField(DECISIONS)
    reason = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    items = serializers.ListField(child=LineReviewIn(), max_length=50, required=False)


class NoteIn(serializers.Serializer):
    note = serializers.CharField(min_length=3, max_length=300)


# ---------------------------------------------------------------- travel

class QuoteIn(serializers.Serializer):
    destinationCategory = serializers.ChoiceField(DESTINATIONS)
    fromDate = IsoDateField()
    toDate = IsoDateField()
    fxRate = _nullish(fx_rate)


class TravelIn(QuoteIn):
    destination = serializers.CharField(min_length=1, max_length=120)
    purpose = serializers.CharField(min_length=3, max_length=1000)
    mode = serializers.ChoiceField(MODES)
    estimatedCost = amount(required=False)
    advanceRequested = amount(required=False)


class TripsQueryIn(serializers.Serializer):
    scope = serializers.ChoiceField(SCOPES, required=False)
    status = serializers.ChoiceField(TRIP_STATUSES, required=False)


class TripReviewIn(serializers.Serializer):
    decision = serializers.ChoiceField(DECISIONS)
    reason = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    advanceApproved = amount(required=False)


class BillIn(serializers.Serializer):
    kind = serializers.ChoiceField(BILL_KINDS)
    billDate = IsoDateField()
    description = serializers.CharField(min_length=1, max_length=300)
    amount = amount()
    fileId = serializers.UUIDField(required=False, allow_null=True)


class SettleIn(serializers.Serializer):
    actualFrom = IsoDateField()
    actualTo = IsoDateField()
    bills = serializers.ListField(child=BillIn(), max_length=60)
    note = serializers.CharField(max_length=1000, required=False, allow_blank=True)


class SettlementReviewIn(serializers.Serializer):
    decision = serializers.ChoiceField(DECISIONS)
    reason = serializers.CharField(max_length=1000, required=False, allow_blank=True)


# ---------------------------------------------------------------- reports

REPORT_FORMATS = ('json', 'csv')


class ReportFormatIn(serializers.Serializer):
    """?format=json|csv on every report (z.enum(['json', 'csv']).optional())."""
    format = serializers.ChoiceField(REPORT_FORMATS, required=False)


class SpendQueryIn(serializers.Serializer):
    from_ = IsoDateField()
    to = IsoDateField()
    groupBy = serializers.ChoiceField(GROUP_BY, default='category')
    format = serializers.ChoiceField(REPORT_FORMATS, required=False)


# The query parameter is `from`, a Python keyword: rename the declared field.
SpendQueryIn._declared_fields = {('from' if k == 'from_' else k): v for k, v in SpendQueryIn._declared_fields.items()}
