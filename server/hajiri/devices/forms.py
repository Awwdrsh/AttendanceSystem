from django import forms
from django.core.validators import RegexValidator

from .constants import COMMANDS, PIN_MESSAGE, PIN_RE
from .serializers import MODALITIES


class ActivateForm(forms.Form):
    name = forms.CharField(label='Name', max_length=120, required=False, help_text='Where the terminal is, so people recognise it.',
                           widget=forms.TextInput(attrs={'placeholder': 'Front door'}))
    branch_id = forms.UUIDField(label='Branch', required=False)


class MapForm(forms.Form):
    device_id = forms.UUIDField(widget=forms.HiddenInput)
    pin = forms.CharField(label='Device PIN', help_text='The user ID enrolled on the terminal.', strip=False,
                          validators=[RegexValidator(PIN_RE, PIN_MESSAGE)])
    employee_id = forms.UUIDField(label='Employee')
    valid_from = forms.DateField(label='Valid from')
    replace = forms.BooleanField(required=False, widget=forms.HiddenInput)


class CommandForm(forms.Form):
    command = forms.ChoiceField(choices=[(c, c) for c in COMMANDS])


class ConsentForm(forms.Form):
    employee_id = forms.UUIDField()
    modality = forms.ChoiceField(choices=[(m, m) for m in MODALITIES])
    notice_version = forms.CharField(max_length=40)
    purpose = forms.CharField(max_length=200, required=False)
    signature = forms.CharField(max_length=4000, required=False)
