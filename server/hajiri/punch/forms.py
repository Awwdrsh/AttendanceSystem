"""Page forms of the punch module."""
from __future__ import annotations

from django import forms

from hajiri.attendance_tools.forms import FilterForm


class FromHomeFilterForm(FilterForm):
    """/attendance/from-home: ?date=&span=day|week&dir=in|all (a future date shows today)."""
    date = forms.DateField(required=False, input_formats=['%Y-%m-%d'])
    span = forms.ChoiceField(choices=[('day', 'day'), ('week', 'week')], required=False, initial='day')
    dir = forms.ChoiceField(choices=[('in', 'in'), ('all', 'all')], required=False, initial='in')
