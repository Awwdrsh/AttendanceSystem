"""Statutory rule versions resolved by date (port of payroll/rules.ts)."""
from __future__ import annotations

from hajiri.core.errors import BadRequestError

from ._db import q

REQUIRED = ['tax.schedule', 'tax.deductions', 'ssf.rates', 'pf.rates', 'ot.multiplier', 'festival.allowance', 'min_wage']
STATUTORY_KEYS = frozenset({'tax.schedule', 'tax.deductions', 'ssf.rates', 'pf.rates', 'ot.multiplier', 'festival.allowance',
                            'min_wage', 'bonus.caps', 'ssf.deposit_days', 'tds.deposit_days'})


def resolve_rules(as_of_ad: str) -> tuple[dict, list[str]]:
    """Every rule as of a date (plan §6.13): the newest version whose validity covers it. Returns (rules, ids)."""
    r = q("""SELECT DISTINCT ON (rule_key) id, rule_key, value FROM rule_version
              WHERE jurisdiction = 'NP' AND valid_from_ad <= $1 AND (valid_to_ad IS NULL OR valid_to_ad >= $1)
              ORDER BY rule_key, valid_from_ad DESC, created_at DESC""", [str(as_of_ad)])
    rules: dict = {}
    ids: list[str] = []
    for row in r:
        rules[row['rule_key']] = row['value']
        ids.append(str(row['id']))
    missing = [k for k in REQUIRED if k not in rules]
    if missing:
        raise BadRequestError(f"statutory rules missing for {as_of_ad}: {', '.join(missing)} (run db:seed:defaults or add rule versions)")
    return rules, ids
