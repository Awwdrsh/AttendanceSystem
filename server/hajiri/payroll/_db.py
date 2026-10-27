"""SQL with the TS placeholders: `$1`, `$2` ... become named psycopg parameters, so a statement copied from the TS
service keeps its shape (a parameter used twice is passed once). Results come back through hajiri.core.db."""
from __future__ import annotations

import re
from decimal import localcontext
from typing import Any, Sequence

from engines.payroll.numeric import ENGINE_CONTEXT, D, js_str, to_dp, to_fixed

from hajiri.core import db

_P = re.compile(r'\$(\d+)')


def _conv(sql: str, params: Sequence[Any]) -> tuple[str, dict[str, Any]]:
    sql = sql.replace('%', '%%')
    return _P.sub(lambda m: f'%(p{m.group(1)})s', sql), {f'p{i + 1}': v for i, v in enumerate(params)}


def q(sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return db.rows(*_conv(sql, params))


def q1(sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    r = q(sql, params)
    return r[0] if r else None


def x(sql: str, params: Sequence[Any] = ()) -> int:
    return db.execute(*_conv(sql, params))


def fx(v: Any, dp: int = 2) -> str:
    """new Decimal(v).toFixed(dp)."""
    with localcontext(ENGINE_CONTEXT):
        return to_fixed(D(v if v is not None else 0), dp)


def dstr(v: Any) -> str:
    """new Decimal(v).toString() ('28.00' -> '28')."""
    with localcontext(ENGINE_CONTEXT):
        return js_str(D(v if v is not None else 0))


def dp2(v: Any) -> Any:
    with localcontext(ENGINE_CONTEXT):
        return to_dp(D(v), 2)


def s(v: Any) -> str | None:
    """A numeric/uuid/date column as the string pg would send (None stays None)."""
    if v is None:
        return None
    return v.isoformat() if hasattr(v, 'isoformat') else str(v)


def js_round(x_: float) -> int:
    """Math.round: halves go up."""
    import math
    return math.floor(x_ + 0.5)
