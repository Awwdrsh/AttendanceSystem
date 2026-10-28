"""Money and day figures as the payroll engine writes them: decimals with the engine's context, amounts as fixed
two-decimal strings, plus the date and row helpers the payroll services share."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import localcontext
from typing import Any

from hajiri.core.numbers import round_half_up  # noqa: F401  (runs and tax_savings import it from here)
from engines.payroll.numeric import ENGINE_CONTEXT, D, js_str, to_dp, to_fixed


def fx(v: Any, dp: int = 2) -> str:
    """An amount as a fixed-point string with `dp` decimals ('1234.5' -> '1234.50'); None counts as 0."""
    with localcontext(ENGINE_CONTEXT):
        return to_fixed(D(v if v is not None else 0), dp)


def plain_number(v: Any) -> str:
    """A decimal without trailing zeros ('28.00' -> '28'); None counts as 0."""
    with localcontext(ENGINE_CONTEXT):
        return js_str(D(v if v is not None else 0))


def dp2(v: Any) -> Any:
    with localcontext(ENGINE_CONTEXT):
        return to_dp(D(v), 2)




def iso(v: Any) -> str | None:
    """A date (or the date part of a timestamp) as 'YYYY-MM-DD'; None stays None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def iso_ms(d: datetime | None) -> str | None:
    """A timestamp as ISO 8601 UTC with milliseconds (the JSON timestamps of the API)."""
    if d is None:
        return None
    d = d.astimezone(timezone.utc)
    return d.strftime('%Y-%m-%dT%H:%M:%S.') + f'{d.microsecond // 1000:03d}Z'


def range_text(r) -> str | None:
    """A date range as PostgreSQL prints it ('[2026-01-01,)', '[2026-01-01,2026-04-01)', 'empty')."""
    if r is None:
        return None
    if getattr(r, 'isempty', False):
        return 'empty'
    lo = '' if r.lower is None else r.lower.isoformat()
    hi = '' if r.upper is None else r.upper.isoformat()
    return f"{'[' if r.lower_inc else '('}{lo},{hi}{']' if r.upper_inc else ')'}"


def strs(r: dict, *keys: str) -> dict:
    """Ids (and other values) of a row as strings, the shape the TS services returned."""
    for k in keys:
        if r.get(k) is not None:
            r[k] = str(r[k])
    return r


def column_row(model, values: dict) -> dict:
    """A `.values()` row keyed by column names (`verified_by_id` -> `verified_by` where the column is named so), the
    shape of `SELECT *`."""
    rename = {f.attname: f.column for f in model._meta.concrete_fields if f.attname != f.column}
    return {rename.get(k, k): v for k, v in values.items()}


def all_columns(model) -> list[str]:
    """Every concrete column of a model as `.values()` names (SELECT *)."""
    return [f.attname for f in model._meta.concrete_fields]
