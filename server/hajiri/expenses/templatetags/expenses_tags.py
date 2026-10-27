"""Template helpers for the expenses pages (shared.tsx money: NPR without paisa for whole amounts)."""
from __future__ import annotations

from django import template

register = template.Library()


@register.filter
def exmoney(value) -> str:
    """{{ v|exmoney }}: 'NPR 1,100' / 'NPR 1,100.50' / '—', as shared.tsx money()."""
    from hajiri.expenses.views import money
    return money(value)
