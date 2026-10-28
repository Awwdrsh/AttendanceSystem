"""
Page inputs of the Insights and report builder pages, read with Django forms: the range and filter row of /insights
and the save dialog of the report builder.
"""
from __future__ import annotations

from django import forms

from hajiri.core.errors import BadRequestError

RANGES = (('6', '6 months'), ('12', '12 months'), ('24', '24 months'))


class InsightsQueryForm(forms.Form):
    """?range=6|12|24&branchId=&orgUnitId=. An unknown range falls back to 12 months; the ids are checked against the
    company by the service (so a foreign id is 'unknown branch', not silently dropped)."""
    range = forms.ChoiceField(choices=RANGES, required=False)
    branchId = forms.CharField(required=False)
    orgUnitId = forms.CharField(required=False)

    def query(self) -> tuple[str, dict]:
        """(range key, the filters without the months) as the service reads them."""
        self.is_valid()
        data = getattr(self, 'cleaned_data', {})
        rng = data.get('range') or '12'
        return rng, {k: data[k] for k in ('branchId', 'orgUnitId') if data.get(k)}


class SaveReportForm(forms.Form):
    """The builder's save dialog: name, description, shared and the definition the dialog carries as JSON."""
    name = forms.CharField(required=False)
    description = forms.CharField(required=False)
    shared = forms.BooleanField(required=False)
    definition = forms.JSONField(required=False)
    as_new = forms.BooleanField(required=False)

    # The dialog's switch posts '1' and the button value '1': read them as the page always did ('0' is not shared).
    def clean_shared(self) -> bool:
        return self.data.get('shared') in ('1', 'on', 'true')

    def clean_as_new(self) -> bool:
        return self.data.get('as_new') == '1'

    def body(self) -> dict:
        """The saved-report body the reports service validates (name rules and the definition stay its job)."""
        if not self.is_valid():
            if 'definition' in self.errors:
                raise BadRequestError('definition is not valid')
        data = self.cleaned_data
        # A missing definition is an empty one; anything posted (even JSON null) goes to the reports service as is.
        definition = data.get('definition') if self.data.get('definition') else {}
        return {'name': data.get('name') or '', 'description': data.get('description') or None,
                'shared': bool(data.get('shared')), 'definition': definition}
