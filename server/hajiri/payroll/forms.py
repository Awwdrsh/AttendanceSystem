"""
Page forms of payroll. A form reads the HTML post (checkboxes, number inputs, repeated selects, empty fields) and
gives the service its TS-shaped body; the rules themselves (lengths, ranges, choices) live once in serializers.py and
are checked by the service, so a page post and a JSON call are refused with the same messages.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

from django import forms

from .serializers import DECL_FIELDS

AMOUNT_RE = re.compile(r'^\d+(\.\d{1,2})?$')
DECL_COLUMNS = {'lifePremium': 'life_premium', 'healthPremium': 'health_premium', 'citMonthly': 'cit_monthly',
                'otherRetirementAnnual': 'other_retirement_annual', 'medicalCost': 'medical_cost', 'donation': 'donation'}


class WholeNumberField(forms.Field):
    """A number input: the whole number typed, or None when it is blank or not a number (the service then reports it)."""

    def to_python(self, value):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None


class AmountField(forms.Field):
    """A money input as the decimal string the API expects ('1,500' -> '1500.00'); blank is '0' (or `blank`)."""

    def __init__(self, *, blank: str | None = '0', **kw):
        self.blank = blank
        kw.setdefault('required', False)
        super().__init__(**kw)

    def to_python(self, value):
        s = (value.strip() if isinstance(value, str) else '').replace(',', '')
        if not s:
            return self.blank
        try:
            return f'{Decimal(s):.2f}'
        except InvalidOperation:
            return s


class TextField(forms.CharField):
    """A text input, trimmed, '' when left out."""

    def __init__(self, **kw):
        kw.setdefault('required', False)
        super().__init__(**kw)


class RawTextField(TextField):
    """A text input exactly as typed (the service trims where the TS schema did)."""

    def __init__(self, **kw):
        super().__init__(strip=False, **kw)


class ValuesField(forms.Field):
    """A repeated input (checkboxes or hidden inputs with one name): the non-blank values, in order."""
    widget = forms.MultipleHiddenInput

    def to_python(self, value):
        return [v for v in (value or []) if isinstance(v, str) and v.strip()]


class PayrollForm(forms.Form):
    """A form bound to a post whose fields never fail on their own: `value()` reads the cleaned data."""

    def __init__(self, data=None, **kw):
        super().__init__(data if data is not None else {}, **kw)
        self.is_valid()

    def value(self, name, default=None):
        v = self.cleaned_data.get(name)
        return default if v is None or v == '' else v


# ---------------------------------------------------------------- query strings of the pages

class PageQuery(PayrollForm):
    """The query parameters the payroll pages read."""
    fy = WholeNumberField(required=False)
    step = WholeNumberField(required=False)
    bs_year = WholeNumberField(required=False)
    bs_month = WholeNumberField(required=False)
    employee_id = TextField()
    employeeId = TextField()
    status = TextField()
    kind = TextField()
    tab = TextField()
    run_id = TextField()
    as_of = TextField()
    code = TextField()
    structure_version_id = TextField()
    download = TextField()

    def employee(self) -> str | None:
        return self.value('employee_id') or self.value('employeeId')


# ---------------------------------------------------------------- pay runs

class PeriodFields(PayrollForm):
    bs_year = WholeNumberField(required=False)
    bs_month = WholeNumberField(required=False)

    def period(self) -> dict:
        return {'bsYear': self.value('bs_year'), 'bsMonth': self.value('bs_month')}


class RunForm(PeriodFields):
    run_type = TextField()
    references_run_id = TextField()
    employee_id = TextField()
    notice_pay = TextField()

    def run(self, period_id: str) -> dict:
        body = {'periodId': period_id, 'runType': self.value('run_type', 'regular')}
        if self.value('references_run_id') and body['runType'] in ('arrears', 'supplementary'):
            body['referencesRunId'] = self.value('references_run_id')
        if body['runType'] == 'final_settlement':
            if self.value('employee_id'):
                body['employeeIds'] = [self.value('employee_id')]
            if self.value('notice_pay'):
                body['noticePay'] = self.value('notice_pay')
        return body


class BonusForm(PeriodFields):
    fiscal_year_start = WholeNumberField(required=False)
    net_profit = AmountField()

    def bonus(self, period_id: str) -> dict:
        return {'periodId': period_id, 'fiscalYearStart': self.value('fiscal_year_start'), 'netProfit': self.value('net_profit')}


class ReviewForm(PayrollForm):
    notes = TextField()

    def review(self) -> dict:
        return {'notes': self.value('notes')} if self.value('notes') else {}


class OverrideForm(PayrollForm):
    reason = TextField()

    def compute(self) -> dict:
        return {'overrideReason': self.value('reason', '')}


class InputForm(PayrollForm):
    employee_id = TextField()
    component_code = TextField()
    amount = TextField()
    note = TextField()

    def inputs(self) -> dict:
        item = {'employeeId': self.value('employee_id', ''), 'componentCode': self.value('component_code', 'ALLOWANCE'),
                'amount': self.value('amount', '')}
        if self.value('note'):
            item['note'] = self.value('note')
        return {'inputs': [item]}


class HoldForm(PayrollForm):
    employee_id = TextField()
    reason = TextField()

    def hold(self) -> dict:
        return {'employeeId': self.value('employee_id', ''), 'reason': self.value('reason', '')}


# ---------------------------------------------------------------- salary structures and assignments

class ComponentForm(PayrollForm):
    code = TextField()
    name_en = RawTextField()
    name_ne = RawTextField()
    type = TextField()
    formula = RawTextField()
    condition = RawTextField()
    depends = forms.BooleanField(required=False)
    taxable = forms.BooleanField(required=False)
    rounding = TextField()
    sort_order = WholeNumberField(required=False)
    active = forms.BooleanField(required=False)

    def component(self, *, is_new: bool, statutory: bool) -> dict:
        b = {'nameEn': self.value('name_en', ''), 'condition': self.value('condition', ''), 'dependsOnPaymentDays': self.value('depends', False),
             'taxable': self.value('taxable', False), 'rounding': self.value('rounding', 'paisa'), 'sortOrder': self.value('sort_order', 0),
             'active': self.value('active', False), 'nameNe': self.value('name_ne', '')}
        if is_new:
            b['code'] = re.sub(r'[^A-Z0-9_]', '', self.value('code', '').upper())
        if not statutory:
            # Statutory components keep their type and formula: the fields are disabled, so the browser does not send them.
            b['type'] = self.value('type', 'earning')
            b['formula'] = self.value('formula', '')
        return b


class StructureForm(PayrollForm):
    code = TextField()
    name_en = RawTextField()
    components = ValuesField(required=False)

    def code_value(self) -> str:
        return self.value('code', '').upper()

    def structure(self) -> dict:
        return {'code': self.code_value(), 'nameEn': self.value('name_en', ''), 'components': self.version()['components']}

    def version(self) -> dict:
        return {'components': [{'code': c} for c in self.cleaned_data.get('components') or []]}


class AssignmentForm(PayrollForm):
    """Base amounts arrive as amount_<CODE> inputs, one per component of the chosen structure version."""
    employee_id = TextField()
    structure_version_id = TextField()

    def assignment(self) -> dict:
        base = {k[len('amount_'):]: v.strip() for k, v in self.data.items()
                if k.startswith('amount_') and isinstance(v, str) and AMOUNT_RE.match(v.strip())}
        return {'employeeId': self.value('employee_id', ''), 'structureVersionId': self.value('structure_version_id', ''),
                'from': (self.data.get('from') or ''), 'baseAmounts': base}


# ---------------------------------------------------------------- declarations, loans, CIT

class DeclarationForm(PayrollForm):
    fy = WholeNumberField(required=False)
    employee_id = TextField()
    life_premium = AmountField()
    health_premium = AmountField()
    cit_monthly = AmountField()
    other_retirement_annual = AmountField()
    medical_cost = AmountField()
    donation = AmountField()

    def amounts(self, *, floor_zero: bool = False) -> dict:
        out = {}
        for key in DECL_FIELDS:
            v = self.value(DECL_COLUMNS[key], '0')
            if floor_zero:
                try:
                    if Decimal(v) < 0:
                        v = '0.00'
                except InvalidOperation:
                    pass
            out[key] = v
        return out

    def declaration(self, user_employee_id: str | None) -> dict:
        employee_id = self.value('employee_id') or user_employee_id
        body = {'fiscalYearStart': self.value('fy'), 'submit': True, **self.amounts()}
        if employee_id != user_employee_id:
            body['employeeId'] = employee_id
        return body


class VerifyForm(PayrollForm):
    employee_id = TextField()
    fy = WholeNumberField(required=False)
    decision = TextField()

    def decision_value(self) -> str:
        return 'verified' if self.value('decision') == 'verified' else 'rejected'

    def verify(self) -> dict:
        return {'fiscalYearStart': self.value('fy'), 'decision': self.decision_value()}


class LoanForm(PayrollForm):
    employee_id = TextField()
    kind = TextField()
    principal = AmountField()
    emi = AmountField()
    start_bs_year = WholeNumberField(required=False)
    start_bs_month = WholeNumberField(required=False)
    note = TextField()

    def loan(self) -> dict:
        body = {'employeeId': self.value('employee_id', ''), 'kind': self.value('kind', 'loan'), 'principal': self.value('principal'),
                'emi': self.value('emi'), 'startBsYear': self.value('start_bs_year'), 'startBsMonth': self.value('start_bs_month')}
        if self.value('note'):
            body['note'] = self.value('note')
        return body


class CitForm(PayrollForm):
    from_bs_month = WholeNumberField(required=False)
    monthly_cit = AmountField()

    def cit(self, employee_id: str) -> dict:
        month = self.value('from_bs_month')
        return {'employeeId': employee_id, 'monthlyCit': self.value('monthly_cit'), **({'fromBsMonth': month} if month else {})}


# ---------------------------------------------------------------- held pay and opening balances

class ReleaseForm(PayrollForm):
    mode = TextField()
    note = TextField()

    def release(self) -> dict:
        mode = 'outside' if self.value('mode') == 'outside' else 'payroll'
        return {'mode': mode, **({'note': self.value('note')} if self.value('note') else {})}


class OpeningForm(PayrollForm):
    employee_id = TextField()
    fy = WholeNumberField(required=False)
    gross = AmountField()
    taxable = AmountField()
    tds_paid = AmountField()
    retirement_employee = AmountField()
    retirement_employer = AmountField()
    festival_paid = forms.BooleanField(required=False)
    months = WholeNumberField(required=False)
    source = TextField()
    note = TextField()

    def opening(self) -> dict:
        return {'employeeId': self.value('employee_id', ''), 'fiscalYearStart': self.value('fy'), 'gross': self.value('gross'),
                'taxable': self.value('taxable'), 'tdsPaid': self.value('tds_paid'), 'retirementEmployee': self.value('retirement_employee'),
                'retirementEmployer': self.value('retirement_employer'), 'festivalPaid': self.value('festival_paid', False),
                'months': self.value('months', 0), 'source': self.value('source', 'previous_employer'), 'note': self.value('note')}


class OpeningImportForm(PayrollForm):
    csv = forms.CharField(required=False, strip=False)


# ---------------------------------------------------------------- my tax savings

class TaxSavingsForm(DeclarationForm):
    note = TextField()
    base = forms.CharField(required=False, strip=False)
    proof_file_ids = ValuesField(required=False)

    def proposed(self) -> dict:
        """The six figures; a negative entry counts as 0."""
        return self.amounts(floor_zero=True)

    def base_figures(self) -> dict:
        try:
            v = json.loads(self.value('base') or '{}')
        except ValueError:
            return {}
        return v if isinstance(v, dict) else {}

    def submission(self) -> dict:
        body = {**self.proposed(), 'fiscalYearStart': self.value('fy'), 'proofFileIds': self.cleaned_data['proof_file_ids']}
        if self.value('note'):
            body['note'] = self.value('note')
        return body
