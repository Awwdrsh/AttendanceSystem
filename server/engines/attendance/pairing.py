"""IN/OUT pairing of punches (port of packages/attendance-engine/src/pairing.ts)."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Optional

from pyuca import Collator

from .models import Pair, PairingMode, Punch

# String.prototype.localeCompare under Node's ICU (en-US = CLDR root collation, tertiary strength), which the TS uses to
# order punches with the same timestamp. The Unicode Collation Algorithm through pyuca gives the same order: it agrees
# with all 3,000 recorded Node comparisons in the parity fixture and with the earlier hand-written ASCII table on a
# 200,000-pair fuzz, and unlike that table it also orders non-ASCII ids the way ICU does (accents, scripts).
@lru_cache(maxsize=1)
def _collator() -> Collator:
    return Collator()


@lru_cache(maxsize=4096)
def _collation_key(s: str) -> tuple[int, ...]:
    return _collator().sort_key(s)


def locale_compare(a: str, b: str) -> int:
    """a.localeCompare(b) as -1, 0 or 1. Strings that differ only in ignorable characters (C0 controls) compare
    equal, as in ICU; there is deliberately no code-point tie-break."""
    if a == b:
        return 0
    ka, kb = _collation_key(a), _collation_key(b)
    return (ka > kb) - (ka < kb)


def sort_punches(punches: Iterable[Punch]) -> list[Punch]:
    """Stable sort by time, then id (a.at - b.at || a.id.localeCompare(b.id), as the TS comparator does)."""
    return sorted(punches, key=lambda p: (p.at, _collation_key(p.id)))


@dataclass(frozen=True, slots=True)
class PairingResult:
    pairs: tuple[Pair, ...]
    # Punches that could not be paired, in order. The last one being an IN means a missing OUT.
    unpaired: tuple[Punch, ...]


def pair_punches(punches: Iterable[Punch], mode: PairingMode) -> PairingResult:
    """Pair ordered punches into IN/OUT intervals.
    - alternating: IN, OUT, IN, OUT... regardless of device direction.
    - strict_log_type: honour device direction; a second IN while one is open is ignored (first IN wins); an OUT with
      nothing open is ignored. Punches with unknown direction fall back to alternating roles.
    - first_last: one pair from the first and last punch (breaks included).
    Any other mode is treated as strict_log_type, as in the TS.
    """
    pairs: list[Pair] = []
    unpaired: list[Punch] = []
    lst = sort_punches(punches)

    if mode == 'first_last':
        if len(lst) >= 2:
            first, last = lst[0], lst[-1]
            pairs.append(Pair(first.at, last.at, first.id, last.id))
        elif len(lst) == 1:
            unpaired.append(lst[0])
        return PairingResult(tuple(pairs), tuple(unpaired))

    if mode == 'alternating':
        for i in range(0, len(lst), 2):
            a = lst[i]
            if i + 1 < len(lst):
                b = lst[i + 1]
                pairs.append(Pair(a.at, b.at, a.id, b.id))
            else:
                unpaired.append(a)
        return PairingResult(tuple(pairs), tuple(unpaired))

    # strict_log_type
    open_: Optional[Punch] = None
    for p in lst:
        role = ('out' if open_ else 'in') if p.direction == 'unknown' else p.direction
        if role == 'in':
            if open_:
                unpaired.append(p)
            else:
                open_ = p
        elif open_:
            pairs.append(Pair(open_.at, p.at, open_.id, p.id))
            open_ = None
        else:
            unpaired.append(p)
    if open_:
        unpaired.append(open_)
    return PairingResult(tuple(pairs), tuple(unpaired))
