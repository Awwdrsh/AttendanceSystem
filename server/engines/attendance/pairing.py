"""IN/OUT pairing of punches (port of packages/attendance-engine/src/pairing.ts)."""
from __future__ import annotations

from dataclasses import dataclass
from functools import cmp_to_key
from typing import Iterable, Optional

from .models import Pair, PairingMode, Punch

# String.prototype.localeCompare under Node's ICU (en-US = CLDR root collation, tertiary strength, punctuation not
# ignorable), which the TS uses to order punches with the same timestamp. Reproduced exactly for ASCII ids: these ASCII
# characters in primary order (a letter and its capital share a weight; lowercase sorts first at the tertiary level),
# C0 controls other than \t-\r and DEL ignored. Measured from Node 26 / ICU 78 and checked by the parity fixture.
_PRIMARY_ORDER = '\t\n\x0b\x0c\r _-,;:!?.\'"()[]{}@*/\\&#%`^+<=>|~$0123456789abcdefghijklmnopqrstuvwxyz'
_PRIMARY = {c: i for i, c in enumerate(_PRIMARY_ORDER)}
for _c in 'abcdefghijklmnopqrstuvwxyz':
    _PRIMARY[_c.upper()] = _PRIMARY[_c]


def _collation_key(s: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    chars = [c for c in s if c in _PRIMARY]
    return tuple(_PRIMARY[c] for c in chars), tuple(1 if 'A' <= c <= 'Z' else 0 for c in chars)


def locale_compare(a: str, b: str) -> int:
    """a.localeCompare(b) as -1, 0 or 1. Exact for ASCII strings. With any non-ASCII character it falls back to code
    point order, which can differ from ICU (accents, scripts, case of non-Latin letters); punch ids are ASCII."""
    if a == b:
        return 0
    if a.isascii() and b.isascii():
        ka, kb = _collation_key(a), _collation_key(b)
    else:
        ka, kb = a, b
    return (ka > kb) - (ka < kb)


def _cmp_punch(a: Punch, b: Punch) -> int:
    # a.at - b.at || a.id.localeCompare(b.id)
    if a.at != b.at:
        return -1 if a.at < b.at else 1
    return locale_compare(a.id, b.id)


def sort_punches(punches: Iterable[Punch]) -> list[Punch]:
    """Stable sort by time, then id, as the TS comparator does."""
    return sorted(punches, key=cmp_to_key(_cmp_punch))


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
