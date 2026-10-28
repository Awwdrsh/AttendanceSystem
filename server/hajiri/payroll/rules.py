"""Statutory rule versions resolved by date (port of payroll/rules.ts)."""
from __future__ import annotations

from django.db.models import Q

from hajiri.core.errors import BadRequestError
from hajiri.db.models import RuleVersion

REQUIRED = ['tax.schedule', 'tax.deductions', 'ssf.rates', 'pf.rates', 'ot.multiplier', 'festival.allowance', 'min_wage']
STATUTORY_KEYS = frozenset({'tax.schedule', 'tax.deductions', 'ssf.rates', 'pf.rates', 'ot.multiplier', 'festival.allowance',
                            'min_wage', 'bonus.caps', 'ssf.deposit_days', 'tds.deposit_days'})


def rules_in_force(as_of_ad):
    """Rule versions whose validity covers the date (rule versions are jurisdiction-wide, not per company)."""
    return RuleVersion.objects.filter(Q(valid_to_ad__isnull=True) | Q(valid_to_ad__gte=as_of_ad), valid_from_ad__lte=as_of_ad)


def resolve_rules(as_of_ad: str) -> tuple[dict, list[str]]:
    """Every rule as of a date (plan §6.13): the newest version whose validity covers it. Returns (rules, ids)."""
    newest = (rules_in_force(str(as_of_ad)).filter(jurisdiction='NP')
              .order_by('rule_key', '-valid_from_ad', '-created_at').distinct('rule_key').values('id', 'rule_key', 'value'))
    rules: dict = {}
    ids: list[str] = []
    for row in newest:
        rules[row['rule_key']] = row['value']
        ids.append(str(row['id']))
    missing = [k for k in REQUIRED if k not in rules]
    if missing:
        raise BadRequestError(f"statutory rules missing for {as_of_ad}: {', '.join(missing)} (run db:seed:defaults or add rule versions)")
    return rules, ids
