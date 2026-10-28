"""django-filter FilterSets of the engage lists. The query is validated first (serializers.py) so a bad value is
refused with the TS message; the FilterSet then narrows the company-scoped queryset."""
from __future__ import annotations

import django_filters

from hajiri.db.models import Suggestion


class SuggestionFilter(django_filters.FilterSet):
    """HR's suggestion box: by status (new, reviewed, answered, archived)."""
    status = django_filters.ChoiceFilter(choices=Suggestion.Status.choices)

    class Meta:
        model = Suggestion
        fields = ['status']
