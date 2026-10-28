"""
Page forms of expenses and travel. A form reads the HTML post (repeated line inputs, number inputs, checkboxes, empty
selects) and gives the service its TS-shaped body; the rules themselves (lengths, amounts, choices) live once in
serializers.py and are checked by the service, so a page post and a JSON call are refused with the same messages.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import forms

from hajiri.db.models import ExpenseCategory, TravelRequest

from .serializers import DESTINATIONS, GROUP_BY


def decimal_text(v: str | None) -> str:
    """What the NumberInput kept: digits and the decimal point ('1,500' -> '1500')."""
    return ''.join(ch for ch in (v or '') if ch.isdigit() or ch == '.')


class _Form(forms.Form):
    """Every field is optional text: the service validates the body and reports problems in the TS wording."""

    def values(self) -> dict:
        self.is_valid()
        return self.cleaned_data

    def lists(self, *names: str) -> list[list[str]]:
        """Repeated inputs (one per line of the form), read as parallel lists."""
        getlist = getattr(self.data, 'getlist', lambda k: [])
        return [getlist(n) for n in names]


def _at(values: list[str], i: int) -> str:
    return values[i] if i < len(values) else ''


# ---------------------------------------------------------------- claims

class ClaimLinesForm(_Form):
    """The lines of the claim form (ClaimItemInput): amount lines carry no km, mileage lines no amount."""
    claim_id = forms.CharField(required=False)

    def items(self, categories: list[dict]) -> list[dict]:
        kinds = {c['id']: c['kind'] for c in categories}
        dates, cats, descs, amounts, kms, receipts = self.lists('expense_date', 'category_id', 'description', 'amount', 'km',
                                                                'receipt_file_id')
        out = []
        for i in range(len(descs)):
            it: dict = {'description': _at(descs, i).strip()}
            if _at(dates, i):
                it['expenseDate'] = _at(dates, i)
            if _at(cats, i):
                it['categoryId'] = _at(cats, i)
            it['receiptFileId'] = _at(receipts, i) or None
            if kinds.get(_at(cats, i)) == ExpenseCategory.Kind.MILEAGE:
                km = _at(kms, i).strip()
                if km:
                    try:
                        it['km'] = float(km)
                    except ValueError:
                        it['km'] = km           # the serializer reports it as not a number
            elif _at(amounts, i).strip():
                it['amount'] = _at(amounts, i).strip()
            out.append(it)
        return out

    def check_body(self, categories: list[dict]) -> dict:
        body: dict = {'items': self.items(categories)}
        if self.values().get('claim_id'):
            body['claimId'] = self.cleaned_data['claim_id']
        return body


class ClaimForm(ClaimLinesForm):
    title = forms.CharField(required=False)
    action = forms.CharField(required=False)

    @property
    def submitting(self) -> bool:
        return self.values().get('action') == 'submit'

    def claim(self, categories: list[dict]) -> dict:
        d = self.values()
        return {'title': d['title'], 'items': self.items(categories), 'submit': self.submitting}


class DecisionForm(_Form):
    decision = forms.CharField(required=False)
    reason = forms.CharField(required=False)

    def decision_body(self) -> dict:
        d = self.values()
        body: dict = {'decision': d['decision']}
        if d['reason']:
            body['reason'] = d['reason']
        return body


class ClaimReviewForm(DecisionForm):
    """Approve or reject; on approval, lines with an amount typed in `adj-<item id>` (and `adjreason-<item id>`)."""

    def review(self) -> dict:
        body = self.decision_body()
        if body['decision'] == 'approve':
            items = []
            for key, v in self.data.items():
                if key.startswith('adj-') and v.strip() != '':
                    item_id = key[4:]
                    it = {'id': item_id, 'approvedAmount': v.strip()}
                    reason = (self.data.get(f'adjreason-{item_id}') or '').strip()
                    if reason:
                        it['reason'] = reason
                    items.append(it)
            body['items'] = items
        return body


class NoteForm(_Form):
    note = forms.CharField(required=False)

    def note_body(self) -> dict:
        return {'note': self.values()['note']}


class ReviewFilterForm(_Form):
    """The status pills above the review lists (pending by default; an unknown value is the default)."""
    status = forms.CharField(required=False)

    def status_for(self, filters: list[tuple[str, str]]) -> str:
        v = self.values().get('status') or 'pending'
        return v if v in dict(filters) else 'pending'


# ---------------------------------------------------------------- travel

class TravelForm(_Form):
    destination = forms.CharField(required=False)
    destination_category = forms.CharField(required=False)
    purpose = forms.CharField(required=False)
    from_date = forms.CharField(required=False)
    to_date = forms.CharField(required=False)
    mode = forms.CharField(required=False)
    estimated_cost = forms.CharField(required=False)
    advance_requested = forms.CharField(required=False)
    fx_rate = forms.CharField(required=False)

    def travel(self) -> dict:
        d = self.values()
        body = {'destination': d['destination'], 'destinationCategory': d['destination_category'], 'purpose': d['purpose'],
                'fromDate': d['from_date'], 'toDate': d['to_date'], 'mode': d['mode']}
        for field, key in (('estimated_cost', 'estimatedCost'), ('advance_requested', 'advanceRequested')):
            v = decimal_text(d[field])
            if v:
                body[key] = v
        if body['destinationCategory'] == TravelRequest.DestinationCategory.INTERNATIONAL:
            body['fxRate'] = decimal_text(d['fx_rate']) or None
        return body

    def quote(self) -> dict | None:
        """The quote body once the dates (and, abroad, the exchange rate) are complete; None before."""
        b = self.travel()
        ready = b['fromDate'] and b['toDate'] and b['toDate'] >= b['fromDate'] and \
            (b['destinationCategory'] != TravelRequest.DestinationCategory.INTERNATIONAL or b.get('fxRate'))
        if not ready or b['destinationCategory'] not in DESTINATIONS:
            return None
        return {k: b[k] for k in ('destinationCategory', 'fromDate', 'toDate')} | ({'fxRate': b['fxRate']} if b.get('fxRate') else {})


class TripReviewForm(DecisionForm):
    advance_approved = forms.CharField(required=False)

    def review(self) -> dict:
        body = self.decision_body()
        adv = self.cleaned_data['advance_approved']
        if body['decision'] == 'approve' and adv:
            body['advanceApproved'] = adv
        return body


class SettleForm(_Form):
    actual_from = forms.CharField(required=False)
    actual_to = forms.CharField(required=False)
    note = forms.CharField(required=False)

    def bills(self) -> list[dict]:
        kinds, dates, descs, amounts, files = self.lists('bill_kind', 'bill_date', 'bill_description', 'bill_amount', 'bill_file_id')
        out = []
        for i in range(len(kinds)):
            b: dict = {'kind': _at(kinds, i), 'description': _at(descs, i).strip(), 'amount': _at(amounts, i).strip(),
                       'fileId': _at(files, i) or None}
            if _at(dates, i):
                b['billDate'] = _at(dates, i)
            out.append(b)
        return out

    def settlement(self) -> dict:
        d = self.values()
        body = {'actualFrom': d['actual_from'], 'actualTo': d['actual_to'], 'bills': self.bills()}
        if d['note']:
            body['note'] = d['note']
        return body

    def preview_bills(self) -> list[dict]:
        """Bills with a readable, non-negative amount (the running figures skip the rest)."""
        out = []
        for b in self.bills():
            try:
                if b['amount'] and Decimal(b['amount']) >= 0:
                    out.append(b)
            except InvalidOperation:
                pass
        return out


# ---------------------------------------------------------------- policies (HR)

class PolicySettingsForm(_Form):
    mileage_rate_per_km = forms.CharField(required=False)
    limit_mode = forms.CharField(required=False)
    claim_window_days = forms.CharField(required=False)

    def settings(self) -> dict:
        d = self.values()
        days = d['claim_window_days']
        return {'mileageRatePerKm': decimal_text(d['mileage_rate_per_km']), 'limitMode': d['limit_mode'],
                'claimWindowDays': int(days) if days.isdigit() else days}


class CategoryForm(_Form):
    code = forms.CharField(required=False)
    name = forms.CharField(required=False)
    kind = forms.CharField(required=False)
    receipt_required_above = forms.CharField(required=False)
    active = forms.BooleanField(required=False)

    def category(self, *, new: bool) -> dict:
        d = self.values()
        kind = d['kind'] or ExpenseCategory.Kind.AMOUNT
        body: dict = {'name': d['name'], 'kind': kind,
                      'receiptRequiredAbove': None if kind == ExpenseCategory.Kind.MILEAGE
                      else (decimal_text(d['receipt_required_above']) or None)}
        if new:
            body['code'] = d['code'].upper()
        else:
            body['active'] = d['active']
        return body

    def limits(self) -> dict:
        grades, per_claim, per_month = self.lists('limit_grade', 'limit_per_claim', 'limit_per_month')
        return {'limits': [{'grade': g, 'perClaim': decimal_text(pc) or None, 'perMonth': decimal_text(pm) or None}
                           for g, pc, pm in zip(grades, per_claim, per_month)]}


class TadaForm(_Form):
    """One row per grade (`grade`), cells `da|<grade>|<destination>` and `lodging|<grade>|<destination>`; empty cells
    are left out, a half-filled one counts the other figure as 0."""

    def rates(self) -> dict:
        (grades,) = self.lists('grade')
        rates = []
        for g in grades:
            for d in DESTINATIONS:
                da, lodging = decimal_text(self.data.get(f'da|{g}|{d}')), decimal_text(self.data.get(f'lodging|{g}|{d}'))
                if not da and not lodging:
                    continue
                rates.append({'grade': g, 'destination': d, 'daRate': da or '0', 'lodgingRate': lodging or '0'})
        return {'rates': rates}


class SpendFilterForm(_Form):
    """The spend report's date range (default: the last 90 days) and grouping."""
    from_ = forms.CharField(required=False)
    to = forms.CharField(required=False)
    group_by = forms.CharField(required=False)

    def __init__(self, data=None, **kw):
        if data is not None:
            data = data.copy()
            if 'from' in data:
                data['from_'] = data.get('from')
        super().__init__(data, **kw)

    def query(self, today: str, default_from: str, *, strict: bool = False) -> dict:
        d = self.values()
        group_by = d['group_by'] if strict or d['group_by'] in GROUP_BY else 'category'
        return {'from': d['from_'] or default_from, 'to': d['to'] or today, 'groupBy': group_by or 'category'}
