"""
Sorting people's names and other text the way the browser did (`a.localeCompare(b)` in the React app): the Unicode
Collation Algorithm through pyuca, so accents, case and punctuation order like ICU's root collation
("Ángela" sorts with "Angela", "ram" just before "Ram"), instead of Python's code-point or casefold order.

    from hajiri.core.collation import collate_key, locale_compare, sort_by_name

    rows.sort(key=lambda r: collate_key(r['name']))
    sort_by_name(people, key=lambda p: p.name_en)          # in place, returns the list
    sorted(groups, key=collate_key)

Engines keep their own ordering rules (server/engines/ reproduces Node's localeCompare on ASCII ids exactly).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Callable, Iterable, TypeVar

T = TypeVar('T')


@lru_cache(maxsize=1)
def _collator():
    from pyuca import Collator
    return Collator()


@lru_cache(maxsize=8192)
def collate_key(text: str | None) -> tuple:
    """A sort key for text; None and '' sort first. The code-point string breaks the rare UCA ties, so the order is
    total and stable."""
    s = text or ''
    return (_collator().sort_key(s), s)


def locale_compare(a: str | None, b: str | None) -> int:
    """-1, 0 or 1, like String.prototype.localeCompare."""
    ka, kb = collate_key(a), collate_key(b)
    return (ka > kb) - (ka < kb)


def sort_by_name(items: list[T], key: Callable[[T], str | None] = lambda x: x, *, reverse: bool = False) -> list[T]:
    """Sort a list in place by a text key with the collation above; returns the list for chaining."""
    items.sort(key=lambda x: collate_key(key(x)), reverse=reverse)
    return items


def sorted_by_name(items: Iterable[T], key: Callable[[T], str | None] = lambda x: x, *, reverse: bool = False) -> list[T]:
    return sorted(items, key=lambda x: collate_key(key(x)), reverse=reverse)
