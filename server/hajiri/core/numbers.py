"""
Rounding and number display shared by the pages, reports, charts and JSON answers (payroll amounts use the payroll
engine's Decimal helpers instead).

    round_half_up(2.5) == 3, round_half_up(-2.5) == -2, round_half_up(1.25, 1) == 1.3     halves go up
    to_fixed(1.005, 2) == '1.00'           fixed decimals of the exact binary value, ties away from zero
    whole(4.0) == 4                        a whole float as an int, so JSON and CSV show 4 rather than 4.0
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal


def round_half_up(x: float, places: int = 0) -> int | float:
    """Round to `places` decimals with halves going up (towards +infinity); whole results come back as int."""
    if not places:
        return math.floor(x + 0.5)
    f = 10 ** places
    return whole(math.floor(x * f + 0.5) / f)


def to_fixed(x: float, digits: int) -> str:
    """`digits` decimals of the exact binary value of x, an exact tie going to the larger magnitude."""
    x = float(x)
    if x == 0:
        x = 0.0  # JS prints (-0).toFixed(2) as '0.00'
    q = Decimal(x).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    return f'{q:.{digits}f}'


def whole(x: float | int) -> float | int:
    """A float with no fraction as an int (4.0 -> 4); anything else unchanged."""
    if isinstance(x, float) and x.is_integer():
        return int(x)
    return x
