"""Bikram Sambat calendar: conversion to and from AD, month bounds, fiscal years and formatting.

Port of packages/bs-calendar. AD dates are ISO 'YYYY-MM-DD' strings or datetime.date values, read as calendar dates
with no time zone; callers pass the Nepal (NPT) date.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import accumulate
from typing import Iterator

from .data import (
    ANCHOR_AD, ANCHOR_BS, BS_MONTH_DAYS, BS_MONTH_NAMES_EN, BS_MONTH_NAMES_NE, DEFAULT_WEEKLY_OFF_DAY,
    FISCAL_YEAR_START_MONTH, LIBRARY_KNOWN_ERRORS, PROJECTED_BS_YEARS,
)

__all__ = [
    'BS_MONTH_DAYS', 'BS_MONTH_NAMES_EN', 'BS_MONTH_NAMES_NE', 'FISCAL_YEAR_START_MONTH', 'DEFAULT_WEEKLY_OFF_DAY',
    'PROJECTED_BS_YEARS', 'LIBRARY_KNOWN_ERRORS', 'BsDate', 'FiscalYear', 'is_projected_bs_year',
    'supported_bs_years', 'days_in_bs_month', 'days_in_bs_year', 'is_valid_bs_date', 'parse_iso_date', 'bs_to_ad',
    'ad_to_bs', 'add_bs_days', 'bs_month_bounds', 'fiscal_year_of', 'fiscal_year', 'weekday_of',
    'to_devanagari_numerals', 'format_bs', 'parse_bs', 'all_supported_days',
]


class BsRangeError(ValueError):
    """A BS or AD date outside the table, or not a real date. Mirrors the TS RangeError."""


@dataclass(frozen=True, slots=True)
class BsDate:
    year: int
    month: int
    day: int


_YEARS = sorted(BS_MONTH_DAYS)
MIN_YEAR = _YEARS[0]
MAX_YEAR = _YEARS[-1]

# Validate the table once at import: 12 months per year, 365/366 days, contiguous years.
for _y in range(MIN_YEAR, MAX_YEAR + 1):
    _months = BS_MONTH_DAYS.get(_y)
    if _months is None:
        raise RuntimeError(f'BS_MONTH_DAYS missing year {_y} (table must be contiguous)')
    if len(_months) != 12:
        raise RuntimeError(f'BS {_y}: expected 12 months, got {len(_months)}')
    if sum(_months) not in (365, 366):
        raise RuntimeError(f'BS {_y}: year length {sum(_months)} is not 365/366')


def is_projected_bs_year(year: int) -> bool:
    """True when the BS year's month lengths are still a projection."""
    return year in PROJECTED_BS_YEARS


def supported_bs_years() -> dict[str, int]:
    return {'min': MIN_YEAR, 'max': MAX_YEAR}


def _assert_year(year: int) -> tuple[int, ...]:
    months = BS_MONTH_DAYS.get(year)
    if months is None:
        raise BsRangeError(f'BS year {year} is outside the supported range {MIN_YEAR}-{MAX_YEAR}')
    return months


def days_in_bs_month(year: int, month: int) -> int:
    months = _assert_year(year)
    if not isinstance(month, int) or month < 1 or month > 12:
        raise BsRangeError(f'BS month {month} out of range 1-12')
    return months[month - 1]


def days_in_bs_year(year: int) -> int:
    return sum(_assert_year(year))


def is_valid_bs_date(bs: BsDate) -> bool:
    try:
        return isinstance(bs.day, int) and 1 <= bs.day <= days_in_bs_month(bs.year, bs.month)
    except BsRangeError:
        return False


# Days before Baishakh 1 of each year, counted from Baishakh 1 of MIN_YEAR, and days before each month within its year.
_YEAR_STARTS = (0, *accumulate(sum(BS_MONTH_DAYS[y]) for y in _YEARS))
_YEAR_START: dict[int, int] = dict(zip(_YEARS, _YEAR_STARTS))
_TOTAL_DAYS = _YEAR_STARTS[-1]
_MONTH_STARTS: dict[int, tuple[int, ...]] = {y: (0, *accumulate(BS_MONTH_DAYS[y])) for y in _YEARS}


def _ordinal(bs: BsDate) -> int:
    """Days from Baishakh 1 of MIN_YEAR to the given BS date."""
    if not is_valid_bs_date(bs):
        raise BsRangeError(f'Invalid BS date {format_bs(bs)}')
    return _YEAR_START[bs.year] + _MONTH_STARTS[bs.year][bs.month - 1] + bs.day - 1


_ANCHOR_ORDINAL = _ordinal(BsDate(*ANCHOR_BS))
# [0-9] and fullmatch, as in the JS /^(\d{4})-(\d{2})-(\d{2})$/: Python's \d also matches other scripts' digits
# and its $ also matches before a trailing newline.
_ISO = re.compile(r'([0-9]{4})-([0-9]{2})-([0-9]{2})')


def parse_iso_date(ad: str) -> date:
    m = _ISO.fullmatch(ad)
    if not m:
        raise BsRangeError(f'AD date must be YYYY-MM-DD, got {ad}')
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        raise BsRangeError(f'Invalid AD date {ad}') from None


def _as_date(ad: str | date) -> date:
    if isinstance(ad, str):
        return parse_iso_date(ad)
    # A datetime is read by its own calendar date, like the TS version reads a Date as a UTC calendar date.
    return ad.date() if isinstance(ad, datetime) else ad


def bs_to_ad(bs: BsDate) -> str:
    """BS -> AD as 'YYYY-MM-DD'."""
    return (ANCHOR_AD + timedelta(days=_ordinal(bs) - _ANCHOR_ORDINAL)).isoformat()


def ad_to_bs(ad: str | date) -> BsDate:
    """AD ('YYYY-MM-DD', date or datetime) -> BS."""
    d = _as_date(ad)
    n = (d - ANCHOR_AD).days + _ANCHOR_ORDINAL
    if n < 0:
        raise BsRangeError(f'AD date {d.isoformat()} is before supported BS range')
    if n >= _TOTAL_DAYS:
        raise BsRangeError(f'AD date {d.isoformat()} is after supported BS range')
    i = bisect_right(_YEAR_STARTS, n) - 1
    year = _YEARS[i]
    n -= _YEAR_STARTS[i]
    starts = _MONTH_STARTS[year]
    month = bisect_right(starts, n)
    return BsDate(year, month, n - starts[month - 1] + 1)


def add_bs_days(bs: BsDate, days: int) -> BsDate:
    return ad_to_bs(parse_iso_date(bs_to_ad(bs)) + timedelta(days=days))


def bs_month_bounds(year: int, month: int) -> dict[str, str | int]:
    """First and last AD dates of a BS month, plus its length. Used for payroll periods and accrual jobs."""
    days = days_in_bs_month(year, month)
    return {'startAd': bs_to_ad(BsDate(year, month, 1)), 'endAd': bs_to_ad(BsDate(year, month, days)), 'days': days}


@dataclass(frozen=True, slots=True)
class FiscalYear:
    start_year: int
    label: str
    start_ad: str
    end_ad: str
    # True when the following BS year is not in the table yet: end_ad is then start_ad + 364 days (verify each Chaitra).
    approximate: bool


def fiscal_year_of(bs: BsDate) -> FiscalYear:
    """Fiscal year containing a BS date, e.g. Bhadra 2083 -> FY 2083/84."""
    return fiscal_year(bs.year if bs.month >= FISCAL_YEAR_START_MONTH else bs.year - 1)


def fiscal_year(start_year: int) -> FiscalYear:
    end_year = start_year + 1
    end_month = FISCAL_YEAR_START_MONTH - 1
    start_ad = bs_to_ad(BsDate(start_year, FISCAL_YEAR_START_MONTH, 1))
    known = end_year in BS_MONTH_DAYS
    end_ad = (bs_to_ad(BsDate(end_year, end_month, days_in_bs_month(end_year, end_month))) if known
              else (parse_iso_date(start_ad) + timedelta(days=364)).isoformat())
    return FiscalYear(start_year, f'{start_year}/{str(end_year)[-2:]}', start_ad, end_ad, not known)


def weekday_of(ad: str | date) -> int:
    """0 = Sunday ... 6 = Saturday (the JS convention the schema stores), for an AD date."""
    return (_as_date(ad).weekday() + 1) % 7


_DEVANAGARI = str.maketrans('0123456789', '०१२३४५६७८९')


def to_devanagari_numerals(s: str) -> str:
    return s.translate(_DEVANAGARI)


def format_bs(bs: BsDate, numerals: str = 'latin', separator: str = '-') -> str:
    s = f'{bs.year}{separator}{bs.month:02d}{separator}{bs.day:02d}'
    return to_devanagari_numerals(s) if numerals == 'devanagari' else s


def parse_bs(s: str) -> BsDate:
    m = _ISO.fullmatch(s)
    if not m:
        raise BsRangeError(f'BS date must be YYYY-MM-DD, got {s}')
    bs = BsDate(int(m[1]), int(m[2]), int(m[3]))
    if not is_valid_bs_date(bs):
        raise BsRangeError(f'Invalid BS date {s}')
    return bs


def all_supported_days() -> Iterator[tuple[BsDate, str]]:
    """Every supported day as (bs, ad), for seeding the bs_calendar table."""
    d = parse_iso_date(bs_to_ad(BsDate(MIN_YEAR, 1, 1)))
    for year in range(MIN_YEAR, MAX_YEAR + 1):
        for month, n in enumerate(BS_MONTH_DAYS[year], start=1):
            for day in range(1, n + 1):
                yield BsDate(year, month, day), d.isoformat()
                d += timedelta(days=1)
