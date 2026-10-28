"""
Page forms of the attendance tools. A form reads the HTML side (query strings, checkboxes, number inputs, repeated
row checkboxes, the CSV file) and gives the service its TS-shaped body; the rules themselves live once in
serializers.py and are checked by the service, so a page post and a JSON call are refused with the same messages.
Filter forms on GET pages fall back to their defaults instead of failing, as the React pages did.

`PageForm`, `form_issues` and `BsMonthForm` are shared with the field and punch pages.
"""
from __future__ import annotations

from datetime import date

from django import forms

from engines.bs_calendar import ad_to_bs, bs_month_bounds

from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, ValidationError


def form_issues(form: forms.Form, names: dict[str, str] | None = None) -> ValidationError:
    """ValidationError with the form's errors as TS issues; `names` maps form fields to body keys."""
    names = names or {}
    return ValidationError([{'path': names.get(f, f) if f != '__all__' else '', 'message': str(m)}
                            for f, msgs in form.errors.items() for m in msgs])


class PageForm(forms.Form):
    """A form bound to a post that raises instead of re-rendering (these pages answer with a toast)."""
    body_names: dict[str, str] = {}

    def cleaned(self) -> dict:
        if not self.is_valid():
            raise form_issues(self, self.body_names)
        return self.cleaned_data


class FilterForm(forms.Form):
    """A GET filter: every field that does not read falls back to its initial value."""

    def values(self) -> dict:
        self.is_valid()
        out = {}
        for name, field in self.fields.items():
            v = self.cleaned_data.get(name) if name not in self.errors else None
            out[name] = v if v not in (None, '') else self.initial_value(name, field)
        return out

    def initial_value(self, name, field):
        v = self.initial.get(name, field.initial)
        return v() if callable(v) else v


class RepeatedField(forms.Field):
    """A repeated input (row checkboxes, multi-selects); blanks dropped."""
    widget = forms.MultipleHiddenInput

    def to_python(self, value):
        return [v for v in (value or []) if v and str(v).strip()]


def _bs_now():
    return ad_to_bs(npt_today().isoformat())


class BsMonthForm(FilterForm):
    """?y=&m= (or bs_year/bs_month): a BS month in the calendar table, else this month."""
    y = forms.IntegerField(required=False)
    m = forms.IntegerField(required=False, min_value=1, max_value=12)

    def __init__(self, data=None, **kw):
        if data is not None:
            data = {'y': data.get('y') or data.get('bs_year'), 'm': data.get('m') or data.get('bs_month')}
        super().__init__(data, **kw)

    def month(self) -> tuple[int, int]:
        t = _bs_now()
        v = self.values()
        y, m = v['y'] or t.year, v['m'] or t.month
        try:
            bs_month_bounds(y, m)
        except Exception:  # noqa: BLE001 - outside the calendar table
            return t.year, t.month
        return y, m


class TodayFilterForm(FilterForm):
    """Who's in: ?date=&filter="""
    FILTERS = ('all', 'in', 'late', 'absent', 'notyet', 'remote')
    date = forms.DateField(required=False, input_formats=['%Y-%m-%d'])
    filter = forms.ChoiceField(choices=[(f, f) for f in FILTERS], required=False, initial='all')


class ExceptionsFilterForm(FilterForm):
    """Exceptions: ?from=&to=&filter= (from defaults to a week ago)."""
    FILTERS = ('all', 'missing', 'absent', 'late', 'ot')
    to = forms.DateField(required=False, input_formats=['%Y-%m-%d'])
    filter = forms.ChoiceField(choices=[(f, f) for f in FILTERS], required=False, initial='all')

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields['from'] = forms.DateField(required=False, input_formats=['%Y-%m-%d'])


class RecomputeForm(PageForm):
    """Selected exception rows: each value 'employee_id|YYYY-MM-DD'."""
    selected = RepeatedField(required=False)

    def __init__(self, data=None, **kw):
        super().__init__({'selected': data.getlist('exceptions-selected')} if data is not None else None, **kw)

    def days(self) -> list[dict]:
        return [{'employeeId': emp, 'localDate': d}
                for emp, d in (v.split('|', 1) for v in self.cleaned()['selected'] if '|' in v)]


class NudgeForm(PageForm):
    target = forms.CharField(required=False)
    instance_id = forms.CharField(required=False)
    employee_id = forms.CharField(required=False)
    to = forms.CharField(required=False)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields['from'] = forms.CharField(required=False)

    def body(self) -> dict:
        d = self.cleaned()
        if d['target'] == 'approver':
            return {'target': 'approver', 'instanceId': d['instance_id']}
        return {'target': 'employee', 'employeeId': d['employee_id'], 'from': d['from'], 'to': d['to']}


class PolicyForm(PageForm):
    """Attendance rules: late clock-in block, who's out scope and the late-arrival penalty."""
    blockLateClockIn = forms.BooleanField(required=False)
    blockAfterMin = forms.FloatField(required=False)
    whosOutScope = forms.CharField(required=False)
    lp_enabled = forms.BooleanField(required=False)
    lp_lateCount = forms.FloatField(required=False)
    lp_penaltyDays = forms.FloatField(required=False)
    lp_minLateMinutes = forms.FloatField(required=False)
    lp_leaveTypeIds = RepeatedField(required=False)
    body_names = {'lp_lateCount': 'latePenalty.lateCount', 'lp_penaltyDays': 'latePenalty.penaltyDays',
                  'lp_minLateMinutes': 'latePenalty.minLateMinutes'}

    @staticmethod
    def _n(v, default):
        if v is None:
            return default
        return int(v) if float(v).is_integer() else v

    def policy(self) -> dict:
        d = self.cleaned()
        return {
            'blockLateClockIn': d['blockLateClockIn'], 'blockAfterMin': self._n(d['blockAfterMin'], 0),
            'whosOutScope': d['whosOutScope'] or 'team',
            'latePenalty': {'enabled': d['lp_enabled'], 'lateCount': self._n(d['lp_lateCount'], 1),
                            'penaltyDays': self._n(d['lp_penaltyDays'], 0.25),
                            'minLateMinutes': self._n(d['lp_minLateMinutes'], 1), 'leaveTypeIds': d['lp_leaveTypeIds'][:5]},
        }


class ApplyPenaltiesForm(PageForm):
    bsYear = forms.IntegerField(required=False)
    bsMonth = forms.IntegerField(required=False)
    selected = RepeatedField(required=False)

    def __init__(self, data=None, **kw):
        if data is not None:
            data = {'bsYear': data.get('bsYear'), 'bsMonth': data.get('bsMonth'),
                    'selected': data.getlist('late-penalties-selected')}
        super().__init__(data, **kw)

    def body(self) -> dict:
        d = self.cleaned()
        return {'bsYear': d['bsYear'] or 0, 'bsMonth': d['bsMonth'] or 0, **({'employeeIds': d['selected']} if d['selected'] else {})}


class ReverseForm(PageForm):
    reason = forms.CharField(required=False)


class UploadFileForm(PageForm):
    """The CSV file of an attendance upload, read as UTF-8 (a byte-order mark is fine)."""
    MAX_BYTES = 900_000
    file = forms.FileField(required=False)

    def csv(self) -> tuple[str, str]:
        """(text, filename); BadRequestError with the page's messages when the file is missing, too large or not UTF-8."""
        f = self.cleaned()['file']
        if not f:
            raise BadRequestError('Choose a CSV file.')
        if f.size > self.MAX_BYTES:
            raise BadRequestError('The file is too large. Upload at most 2,000 rows at a time.')
        try:
            return f.read().decode('utf-8-sig'), f.name
        except UnicodeDecodeError:
            raise BadRequestError('Save the file as CSV (UTF-8) and choose it again.', {'title': 'Could not read the file'}) from None


class ImportForm(PageForm):
    csv = forms.CharField(required=False, strip=False)
    reason = forms.CharField(required=False)
    filename = forms.CharField(required=False, strip=False)

    def body(self) -> dict:
        d = self.cleaned()
        return {'csv': d['csv'], 'reason': d['reason'], **({'filename': d['filename']} if d['filename'] else {})}


class DayForm(FilterForm):
    """?date= of the day modal (required)."""
    date = forms.DateField(required=False, input_formats=['%Y-%m-%d'])

    def day(self) -> str:
        d = self.values()['date']
        if not isinstance(d, date):
            raise BadRequestError('date: YYYY-MM-DD')
        return d.isoformat()
