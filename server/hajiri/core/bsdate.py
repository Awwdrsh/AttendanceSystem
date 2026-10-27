"""Bikram Sambat labels for display (port of apps/api/src/bsdate.ts). Every date shown to people goes through these."""
from __future__ import annotations

from datetime import date, datetime

from engines.bs_calendar import BS_MONTH_NAMES_EN, BsRangeError, ad_to_bs, format_bs

from .clock import NPT


def _iso(v: str | date | datetime) -> str:
    if isinstance(v, datetime):
        return v.astimezone(NPT).date().isoformat() if v.tzinfo else v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def bs_label(iso: str | date | datetime | None) -> str:
    """'28 Ashwin 2083' for an AD date; the ISO date itself outside the calendar table; '' for None."""
    if not iso:
        return ''
    s = _iso(iso)
    try:
        b = ad_to_bs(s)
        return f'{b.day} {BS_MONTH_NAMES_EN[b.month - 1]} {b.year}'
    except (BsRangeError, ValueError):
        return s


def bs_range(from_: str | date, to: str | date) -> str:
    """'28 Ashwin 2083' or '28 Ashwin 2083 to 2 Kartik 2083'."""
    if _iso(from_) == _iso(to):
        return bs_label(from_)
    return f'{bs_label(from_)} to {bs_label(to)}'


def bs_month_name(year: int, month: int) -> str:
    """'Ashwin 2083'."""
    name = BS_MONTH_NAMES_EN[month - 1] if 1 <= month <= 12 else str(month)
    return f'{name} {year}'


def bs_iso(iso: str | date | None) -> str | None:
    """'2083-06-05' (the stored bs_date format) or None outside the table."""
    if not iso:
        return None
    try:
        return format_bs(ad_to_bs(_iso(iso)))
    except (BsRangeError, ValueError):
        return None
