"""Page forms (the TS pages' client-side checks). Each form also builds the service body (the TS camelCase shape);
the service repeats every rule with the API's messages."""
from __future__ import annotations

from django import forms
from django.core.validators import RegexValidator

from hajiri.db.models import RegularisationRequest

Kind = RegularisationRequest.Kind
Direction = RegularisationRequest.PunchDirection

TIME_RE = r'^([01]\d|2[0-3]):[0-5]\d$'

DIRECTIONS = [(Direction.IN, 'Clock-in'), (Direction.OUT, 'Clock-out')]
KIND_CHOICES = [(Kind.MISSED_PUNCH, 'I forgot to clock in or out'), (Kind.WFH, 'I worked from home'),
                (Kind.ON_DUTY, 'I was on duty elsewhere'), (Kind.HALF_DAY, 'I worked a half day'),
                (Kind.SHIFT_CHANGE, 'I worked a different shift')]


def _npt_instant(local_date, hhmm: str) -> str:
    """'YYYY-MM-DDTHH:MM:00+05:45': a time typed on the page is Nepal time."""
    return f'{local_date.isoformat()}T{hhmm}:00+05:45'


class _Reason(forms.CharField):
    def __init__(self, max_length=1000, **kw):
        super().__init__(min_length=3, max_length=max_length, strip=True, widget=forms.Textarea(attrs={'rows': 2}), **kw)


class FixPunchForm(forms.Form):
    """FixPunchPage.tsx: employee, date, time, punch type and reason."""
    employee_id = forms.UUIDField(label='Employee')
    local_date = forms.DateField(label='Date')
    time = forms.CharField(label='Time', initial='18:00', help_text='24-hour clock, Nepal time',
                           validators=[RegexValidator(TIME_RE, 'Use HH:MM, for example 18:05')],
                           widget=forms.TextInput(attrs={'placeholder': 'HH:MM', 'inputmode': 'numeric'}))
    direction = forms.ChoiceField(label='Punch type', choices=DIRECTIONS, initial=Direction.OUT, help_text='Which punch was missed',
                                  widget=forms.RadioSelect)
    reason = _Reason(label='Reason', help_text='The employee sees this next to the entry.')

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields['reason'].widget.attrs['placeholder'] = 'For example: forgot to clock out after the evening shift'

    def manual_body(self) -> dict:
        """HR: the manual punch body (TS POST /punch/manual)."""
        d = self.cleaned_data
        return {'employeeId': str(d['employee_id']), 'localDate': d['local_date'].isoformat(), 'time': d['time'],
                'direction': d['direction'], 'reason': d['reason']}

    def request_body(self) -> dict:
        """A manager: a missed-punch correction for approval."""
        d = self.cleaned_data
        return {'employeeId': str(d['employee_id']), 'localDate': d['local_date'].isoformat(), 'kind': Kind.MISSED_PUNCH.value,
                'punchTime': _npt_instant(d['local_date'], d['time']), 'punchDirection': d['direction'],
                'reason': d['reason']}


class CorrectionForm(forms.Form):
    """MyAttendancePage.tsx CorrectionForm."""
    employee_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    local_date = forms.DateField(label='Date')
    kind = forms.ChoiceField(label='What needs correcting?', choices=KIND_CHOICES, initial=Kind.MISSED_PUNCH)
    direction = forms.ChoiceField(label='Missing entry', choices=DIRECTIONS, initial=Direction.OUT, required=False)
    time = forms.CharField(label='Time', initial='18:00', required=False, help_text='24-hour, e.g. 18:00',
                           widget=forms.TextInput(attrs={'placeholder': 'HH:MM', 'inputmode': 'numeric'}))
    shift_id = forms.UUIDField(label='Shift worked', required=False)
    reason = _Reason(label='Reason', help_text='Your manager sees this when reviewing the request.')

    time_format = RegexValidator(TIME_RE, 'Use HH:MM, e.g. 09:30')

    def clean(self):
        data = super().clean()
        if data.get('kind') == Kind.MISSED_PUNCH:
            try:
                self.time_format(data.get('time') or '')
            except forms.ValidationError as e:
                self.add_error('time', e)
            if not data.get('direction'):
                self.add_error('direction', 'This field is required.')
        return data

    def request_body(self) -> dict:
        d = self.cleaned_data
        body = {'localDate': d['local_date'].isoformat(), 'kind': d['kind'], 'reason': d['reason']}
        if d.get('employee_id'):
            body['employeeId'] = str(d['employee_id'])
        if d['kind'] == Kind.MISSED_PUNCH:
            body.update({'punchTime': _npt_instant(d['local_date'], d['time']), 'punchDirection': d['direction']})
        if d['kind'] == Kind.SHIFT_CHANGE and d.get('shift_id'):
            body['shiftId'] = str(d['shift_id'])
        return body


class WfhForm(forms.Form):
    """HomePage.tsx 'Work from home' request."""
    local_date = forms.DateField(label='Day')
    reason = _Reason(label='Reason')

    def request_body(self) -> dict:
        return {'localDate': self.cleaned_data['local_date'].isoformat(), 'kind': Kind.WFH.value,
                'reason': self.cleaned_data['reason']}

