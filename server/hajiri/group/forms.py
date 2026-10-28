"""Forms of the group pages: the report filters and the pay visibility switch. Company choices are always the
companies of the viewer's group scope, so a company outside it is an invalid choice."""
from __future__ import annotations

from django import forms

from engines.bs_calendar import BS_MONTH_NAMES_EN, MAX_YEAR, MIN_YEAR

from . import services


def company_choices(scope: services.GroupScope) -> list[tuple[str, str]]:
    return [('', 'All companies')] + [(c.id, c.name) for c in scope.companies]


class ReportForm(forms.Form):
    """?report=&company=&period=month|fy&bs_year=&bs_month=&fy= (and format=csv for the download)."""
    report = forms.ChoiceField(choices=[(k, v[0]) for k, v in services.REPORTS.items()], required=False)
    company = forms.ChoiceField(required=False, label='Company')
    period = forms.ChoiceField(choices=[('month', 'BS month'), ('fy', 'Fiscal year')], required=False)
    bs_year = forms.IntegerField(min_value=MIN_YEAR, max_value=MAX_YEAR, required=False)
    bs_month = forms.IntegerField(min_value=1, max_value=12, required=False)
    fy = forms.IntegerField(min_value=MIN_YEAR, max_value=MAX_YEAR, required=False)

    def __init__(self, data, scope: services.GroupScope, **kw):
        super().__init__(data, **kw)
        self.fields['company'].choices = company_choices(scope)

    def query(self) -> dict:
        """The cleaned filters with defaults; a value that does not read is left at its default."""
        self.is_valid()
        d = {k: v for k, v in getattr(self, 'cleaned_data', {}).items() if v not in (None, '')}
        for name in self.errors:
            d.pop(name, None)
        return {'report': d.get('report') or 'headcount', 'company': d.get('company') or None,
                'period': d.get('period') or 'month', 'bs_year': d.get('bs_year'), 'bs_month': d.get('bs_month'),
                'fy': d.get('fy')}


def fiscal_year_choices(today) -> list[tuple[int, str]]:
    """This fiscal year and the ten before it (within the calendar table)."""
    cur = min(services.current_fiscal_start(today), MAX_YEAR - 1)
    return [(y, services.fiscal_label(y)) for y in range(cur, max(MIN_YEAR, cur - 10) - 1, -1)]


MONTH_CHOICES = list(enumerate(BS_MONTH_NAMES_EN, start=1))


class PayPolicyForm(forms.Form):
    """The active company's switch: hide pay figures from executives in group views."""
    hide = forms.BooleanField(required=False)
