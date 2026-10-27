"""Forms mirroring the zod bodies of the shifts routes (same limits and choices). Views turn cleaned data into the TS
camelCase body and call the service, which validates again."""
from django import forms

HHMM = r'^([01]\d|2[0-3]):[0-5]\d$'
PAIRING = [('alternating', 'Alternating in/out'), ('strict_log_type', 'Use device direction'), ('first_last', 'First in to last out')]
ROUNDING = [('nearest', 'Nearest'), ('down', 'Down'), ('up', 'Up')]
SCOPE = [('all', 'Everyone'), ('female', 'Women only (Teej, Jitiya)')]


def _minutes(label, max_value, help_text=None):
    return forms.IntegerField(label=label, min_value=0, max_value=max_value, help_text=help_text,
                              widget=forms.NumberInput(attrs={'min': 0, 'max': max_value, 'step': 1}))


class ShiftForm(forms.Form):
    code = forms.CharField(label='Code', max_length=20)
    name_en = forms.CharField(label='Name', max_length=80)
    name_ne = forms.CharField(max_length=80, required=False, widget=forms.HiddenInput)
    start = forms.RegexField(label='Starts at', regex=HHMM, error_messages={'invalid': 'HH:MM'},
                             widget=forms.TextInput(attrs={'placeholder': 'HH:MM'}))
    end = forms.RegexField(label='Ends at', regex=HHMM, error_messages={'invalid': 'HH:MM'},
                           help_text='Earlier than the start means a night shift', widget=forms.TextInput(attrs={'placeholder': 'HH:MM'}))
    grace_in_min = _minutes('Grace on arrival', 240, 'Late if clock-in is after start plus grace')
    grace_out_min = _minutes('Grace on leaving', 240)
    early_in_window_min = _minutes('Early clock-in window', 720, 'How early a clock-in still counts')
    late_out_window_min = _minutes('Late clock-out window', 720, 'How late a clock-out still counts')
    break_minutes = _minutes('Fixed break', 240, '30 min after 5 h (Sec 28(2)); 0 measures it from punches')
    half_day_threshold_min = _minutes('Half day if worked under', 720)
    absent_threshold_min = _minutes('Absent if worked under', 720)
    pairing_mode = forms.ChoiceField(label='Pairing', choices=PAIRING, help_text='How punches become in/out pairs')
    rounding_min = _minutes('Round worked time to', 60, '0 turns rounding off')
    rounding_direction = forms.ChoiceField(label='Rounding direction', choices=ROUNDING)

    BLANK = {'code': '', 'name_en': '', 'name_ne': '', 'start': '10:00', 'end': '18:00', 'grace_in_min': 15, 'grace_out_min': 0,
             'early_in_window_min': 60, 'late_out_window_min': 120, 'break_minutes': 0, 'half_day_threshold_min': 240,
             'absent_threshold_min': 120, 'pairing_mode': 'alternating', 'rounding_min': 0, 'rounding_direction': 'nearest'}

    def clean_code(self):
        return self.cleaned_data['code'].upper()

    @staticmethod
    def initial_from(shift: dict) -> dict:
        return {'code': shift['code'], 'name_en': shift['name_en'], 'name_ne': shift['name_ne'] or '', 'start': shift['start'][:5],
                'end': shift['end'][:5], **{k: shift[k] for k in ('grace_in_min', 'grace_out_min', 'early_in_window_min',
                                                                 'late_out_window_min', 'break_minutes', 'half_day_threshold_min',
                                                                 'absent_threshold_min', 'pairing_mode', 'rounding_min',
                                                                 'rounding_direction')}}

    def body(self) -> dict:
        d = self.cleaned_data
        return {'code': d['code'], 'nameEn': d['name_en'], 'nameNe': d['name_ne'] or '', 'start': d['start'], 'end': d['end'],
                'graceInMin': d['grace_in_min'], 'graceOutMin': d['grace_out_min'], 'earlyInWindowMin': d['early_in_window_min'],
                'lateOutWindowMin': d['late_out_window_min'], 'breakMinutes': d['break_minutes'],
                'halfDayThresholdMin': d['half_day_threshold_min'], 'absentThresholdMin': d['absent_threshold_min'],
                'pairingMode': d['pairing_mode'], 'roundingMin': d['rounding_min'], 'roundingDirection': d['rounding_direction']}


class AssignForm(forms.Form):
    employee_id = forms.UUIDField(label='Employee')
    from_date = forms.DateField(label='Starting from')


class HolidayForm(forms.Form):
    local_date = forms.DateField(label='Date')
    name_en = forms.CharField(label='Name', max_length=120)
    name_ne = forms.CharField(max_length=120, required=False, widget=forms.HiddenInput)
    gender_scope = forms.ChoiceField(label='Applies to', choices=SCOPE)
    optional = forms.BooleanField(label='Optional holiday', required=False, help_text='Employees choose whether to take it')

    def body(self) -> dict:
        d = self.cleaned_data
        return {'localDate': d['local_date'].isoformat(), 'nameEn': d['name_en'], 'nameNe': d['name_ne'] or '',
                'genderScope': d['gender_scope'], 'optional': bool(d['optional'])}
