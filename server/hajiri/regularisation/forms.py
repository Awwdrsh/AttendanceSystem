"""Page forms (the TS pages' client-side checks); the service repeats every rule with the API's messages."""
from django import forms
from django.core.validators import RegexValidator

TIME_RE = r'^([01]\d|2[0-3]):[0-5]\d$'

DIRECTIONS = [('in', 'Clock-in'), ('out', 'Clock-out')]
KIND_CHOICES = [('missed_punch', 'I forgot to clock in or out'), ('wfh', 'I worked from home'),
                ('on_duty', 'I was on duty elsewhere'), ('half_day', 'I worked a half day'),
                ('shift_change', 'I worked a different shift')]


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
    direction = forms.ChoiceField(label='Punch type', choices=DIRECTIONS, initial='out', help_text='Which punch was missed',
                                  widget=forms.RadioSelect)
    reason = _Reason(label='Reason', help_text='The employee sees this next to the entry.')

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields['reason'].widget.attrs['placeholder'] = 'For example: forgot to clock out after the evening shift'


class CorrectionForm(forms.Form):
    """MyAttendancePage.tsx CorrectionForm."""
    employee_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    local_date = forms.DateField(label='Date')
    kind = forms.ChoiceField(label='What needs correcting?', choices=KIND_CHOICES, initial='missed_punch')
    direction = forms.ChoiceField(label='Missing entry', choices=DIRECTIONS, initial='out', required=False)
    time = forms.CharField(label='Time', initial='18:00', required=False, help_text='24-hour, e.g. 18:00',
                           widget=forms.TextInput(attrs={'placeholder': 'HH:MM', 'inputmode': 'numeric'}))
    shift_id = forms.UUIDField(label='Shift worked', required=False)
    reason = _Reason(label='Reason', help_text='Your manager sees this when reviewing the request.')

    def clean(self):
        data = super().clean()
        if data.get('kind') == 'missed_punch':
            import re
            if not re.fullmatch(TIME_RE, data.get('time') or ''):
                self.add_error('time', 'Use HH:MM, e.g. 09:30')
            if not data.get('direction'):
                self.add_error('direction', 'This field is required.')
        return data


class WfhForm(forms.Form):
    """HomePage.tsx 'Work from home' request."""
    local_date = forms.DateField(label='Day')
    reason = _Reason(label='Reason')
