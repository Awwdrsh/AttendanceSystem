"""
Page forms of the leave app. A form reads the HTML post (switches, number inputs, empty selects) and gives the service
its TS-shaped body; the rules themselves (lengths, ranges, choices) live once in serializers.py and are checked by the
service, so a page post and a JSON call are refused with the same messages. A value the form cannot even read (text in
a number field) is reported in the same issue shape.
"""
from __future__ import annotations

import copy

from django import forms

from hajiri.core.errors import ValidationError

SWITCH_ON = ('on', 'true', '1')


def form_issues(form: forms.Form, names: dict[str, str] | None = None) -> ValidationError:
    """ValidationError with the form's errors as TS issues; `names` maps form fields to body keys."""
    names = names or {}
    return ValidationError([{'path': names.get(f, f) if f != '__all__' else '', 'message': str(m)}
                            for f, msgs in form.errors.items() for m in msgs])


class SwitchField(forms.Field):
    """A checkbox or switch: on only for 'on', 'true' or '1' (the raw value is read, not CheckboxInput's guess)."""
    widget = forms.HiddenInput

    def to_python(self, value):
        return value in SWITCH_ON


class NumberField(forms.Field):
    """A number input: '' is None, an integral value an int (the TS page sent Number(value))."""

    def to_python(self, value):
        value = (value or '').strip()
        if value == '':
            return None
        try:
            f = float(value)
        except ValueError:
            raise forms.ValidationError('Expected number, received string') from None
        return int(f) if f.is_integer() else f


def _text() -> forms.CharField:
    return forms.CharField(required=False, strip=False)


class _Form(forms.Form):
    """Always bound to a post (or a query string) and raises instead of re-rendering."""
    body_names: dict[str, str] = {}
    extra_fields: dict[str, forms.Field] = {}     # names that cannot be class attributes ('from', camelCase keys)

    def __init__(self, data, *args, **kw):
        super().__init__(data, *args, **kw)
        for name, field in self.extra_fields.items():
            self.fields[name] = copy.deepcopy(field)

    def cleaned(self) -> dict:
        if not self.is_valid():
            raise form_issues(self, self.body_names)
        return self.cleaned_data


class ApplyForm(_Form):
    """LeaveApplyForm: type, dates, half day or hours, reason and attachment reference."""
    leave_type_id = _text()
    to = _text()
    half = SwitchField(required=False)
    half_which = _text()
    hourly = SwitchField(required=False)
    from_time = _text()
    to_time = _text()
    reason = _text()
    attachment = _text()
    override = SwitchField(required=False)
    extra_fields = {'from': _text()}

    def body(self, employee_id: str) -> dict:
        d = self.cleaned()
        b: dict = {'employeeId': employee_id, 'leaveTypeId': d['leave_type_id'], 'from': d['from'],
                   'to': d['to'] or d['from']}
        hourly = d['hourly'] and b['from'] == b['to']
        if d['half'] and not hourly:
            b['halfDay'] = d['half_which'] or 'first_half'
        if hourly:
            b['fromTime'] = d['from_time']
            b['toTime'] = d['to_time']
        if d['reason'].strip():
            b['reason'] = d['reason']
        if d['attachment'].strip():
            b['attachmentKey'] = d['attachment']
        if d['override']:
            b['override'] = True
        return b


class PreviewForm(_Form):
    """The preview's query string; query() is None while the form is incomplete (no type, no dates, to before from,
    bad hours)."""
    leave_type_id = _text()
    to = _text()
    employee_id = _text()
    half = SwitchField(required=False)
    half_which = _text()
    hourly = SwitchField(required=False)
    from_time = _text()
    to_time = _text()
    extra_fields = {'from': _text()}

    def query(self) -> dict | None:
        d = self.cleaned()
        t, frm, to = d['leave_type_id'], d['from'], d['to'] or d['from']
        if not t or not frm or not to or to < frm:
            return None
        q = {'leaveTypeId': t, 'from': frm, 'to': to}
        if d['employee_id']:
            q['employeeId'] = d['employee_id']
        if d['hourly'] and frm == to:
            ft, tt = d['from_time'], d['to_time']
            if not (ft < tt):
                return None
            q.update({'fromTime': ft, 'toTime': tt})
        elif d['half']:
            q['halfDay'] = d['half_which'] or 'first_half'
        return q


class AdjustForm(_Form):
    """LeaveAdjustPage: employee, type, days, kind, effective date and reason."""
    employee_id = _text()
    leave_type_id = _text()
    delta_days = NumberField(required=False)
    kind = _text()
    effective_date = _text()
    reason = _text()
    body_names = {'delta_days': 'deltaDays'}

    def body(self) -> dict:
        d = self.cleaned()
        b: dict = {'employeeId': d['employee_id'], 'leaveTypeId': d['leave_type_id'], 'reason': d['reason'],
                   'kind': d['kind'] or 'adjustment', 'deltaDays': d['delta_days'] if d['delta_days'] is not None else 0}
        if d['effective_date']:
            b['effectiveDate'] = d['effective_date']
        return b


NUMBER_FIELDS = ('accrualDays', 'accrualPerWorkedDays', 'maxBalance', 'expiryDays', 'negativeBalanceLimit',
                 'certificateAfterDays', 'maxConsecutiveDays', 'minServiceDays')
SWITCHES = ('paid', 'allowHalfDay', 'allowHourly', 'sandwichRule', 'carryForward', 'encashable')
LOCKED = ('accrualBasis', 'accrualDays', 'accrualPerWorkedDays', 'maxBalance', 'paid', 'genderScope', 'expiryDays')
TEXTS = ('code', 'nameEn', 'nameNe', 'accrualBasis', 'genderScope')


class TypeForm(_Form):
    """LeaveTypesPage save(): empty values are left out; a statutory type never sends its entitlement fields. The page
    names its inputs after the body keys."""
    extra_fields = {**{k: _text() for k in (*TEXTS, 'clubWithId')}, **{k: NumberField(required=False) for k in NUMBER_FIELDS},
                    **{k: SwitchField(required=False) for k in SWITCHES}}

    def __init__(self, data, *, locked: bool, partial: bool):
        super().__init__(data)
        self.locked, self.partial = locked, partial

    def body(self) -> dict:
        d = self.cleaned()
        b: dict = {}
        for k in TEXTS:
            if d[k] != '':
                b[k] = d[k].upper() if k == 'code' else d[k]
        for k in NUMBER_FIELDS:
            if d[k] is not None:
                b[k] = d[k]
        for k in SWITCHES:
            b[k] = d[k]
        if not b.get('paid', True):
            b['allowHourly'] = False
        b['clubWithId'] = d['clubWithId'] or None
        if self.partial:
            b.pop('code', None)  # the code cannot change once a type exists
        if self.locked:
            for k in LOCKED:
                b.pop(k, None)
        return b
