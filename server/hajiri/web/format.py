"""
Display formatting shared by every page: Bikram Sambat dates with English month names, Nepal times, NPR money with
lakh/crore grouping, durations and relative times. Port of apps/web/src/lib/format.ts and lib/bs.ts. The template
filters in hajiri.web.templatetags.hj are thin wrappers over these functions.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from engines.bs_calendar import (
    BS_MONTH_NAMES_EN, BsDate, ad_to_bs, bs_to_ad, days_in_bs_month, format_bs, supported_bs_years,
    to_devanagari_numerals, weekday_of,
)

from hajiri.core.clock import NPT, now

NPT_OFFSET_MIN = 345
DASH = '—'
MINUS = '−'
AD_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
WEEKDAYS = ('Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat')
_ISO = re.compile(r'^(\d{4})-(\d{2})-(\d{2})')


# ---------------------------------------------------------------- dates

def iso_of(value) -> str | None:
    """'YYYY-MM-DD' from a date, a datetime (taken in Nepal time) or an ISO string; None when empty."""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(NPT)
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def safe_ad_to_bs(ad) -> BsDate | None:
    try:
        return ad_to_bs(iso_of(ad))
    except (ValueError, TypeError):
        return None


def fmt_gregorian(ad) -> str:
    """Gregorian "14 Oct 2026", the small secondary date shown next to a Bikram Sambat date."""
    iso = iso_of(ad)
    if not iso:
        return DASH
    m = _ISO.match(iso)
    if not m:
        return iso
    return f'{int(m[3])} {AD_MONTHS[int(m[2]) - 1]} {m[1]}'


def fmt_date(ad) -> str:
    """The app's date format: Bikram Sambat, e.g. "28 Ashwin 2083". Outside the calendar table: Gregorian."""
    iso = iso_of(ad)
    if not iso:
        return DASH
    bs = safe_ad_to_bs(iso[:10])
    if not bs:
        return fmt_gregorian(iso)
    return f'{bs.day} {BS_MONTH_NAMES_EN[bs.month - 1]} {bs.year}'


fmt_ad = fmt_date


@dataclass(frozen=True)
class DualDate:
    bs: str
    bs_long: str
    ad: str


def dual_date(ad) -> DualDate:
    """Primary BS, muted AD: DualDate(bs='2083-05-12', bs_long='12 Bhadra 2083', ad='28 Aug 2026')."""
    iso = iso_of(ad)
    if not iso:
        return DualDate(DASH, DASH, DASH)
    bs = safe_ad_to_bs(iso)
    if not bs:
        return DualDate(iso, iso, fmt_gregorian(iso))
    return DualDate(format_bs(bs), f'{bs.day} {BS_MONTH_NAMES_EN[bs.month - 1]} {bs.year}', fmt_gregorian(iso))


def bs_month_label(bs_year: int, bs_month: int) -> str:
    return f'{BS_MONTH_NAMES_EN[int(bs_month) - 1]} {int(bs_year)}'


def fiscal_label(ad) -> str:
    """Fiscal year label from an AD date: "2083/84"."""
    bs = safe_ad_to_bs(ad)
    if not bs:
        return ''
    start = bs.year if bs.month >= 4 else bs.year - 1
    return f'{start}/{str(start + 1)[-2:]}'


def fmt_date_long(ad) -> str:
    """"Tue, 27 Ashwin 2083"."""
    iso = iso_of(ad)
    if not iso:
        return DASH
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return str(ad)
    return f'{WEEKDAYS[(d.weekday() + 1) % 7]}, {fmt_date(iso)}'


def today_npt() -> str:
    return now().astimezone(NPT).date().isoformat()


def fmt_date_short(ad) -> str:
    """"27 Ashwin" within the current BS year, otherwise "27 Ashwin 2082"."""
    iso = iso_of(ad)
    if not iso:
        return DASH
    full = fmt_date(iso)
    bs, cur = safe_ad_to_bs(iso), safe_ad_to_bs(today_npt())
    return re.sub(r' \d{4}$', '', full) if bs and cur and bs.year == cur.year else full


def _instant(value) -> datetime | None:
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        s = str(value).replace('Z', '+00:00')
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fmt_time(value) -> str:
    """HH:MM in Nepal time for an instant (datetime or ISO string)."""
    d = _instant(value)
    if d is None:
        return DASH
    return d.astimezone(NPT).strftime('%H:%M')


def fmt_datetime(value) -> str:
    """"28 Ashwin 2083, 10:17" in Nepal time."""
    d = _instant(value)
    if d is None:
        return DASH
    local = d.astimezone(NPT)
    return f'{fmt_date(local.date())}, {local.strftime("%H:%M")}'


def relative_time(value, at: datetime | None = None) -> str:
    d = _instant(value)
    if d is None:
        return DASH
    diff = ((at or now()) - d).total_seconds() * 1000
    minutes = _round_half_up(diff / 60_000)
    if minutes < 1:
        return 'just now'
    if minutes < 60:
        return f'{minutes} min ago'
    h = _round_half_up(minutes / 60)
    if h < 48:
        return f'{h} h ago'
    return f'{_round_half_up(h / 24)} d ago'


def greeting(at: datetime | None = None) -> str:
    h = (at or now()).astimezone(NPT).hour
    return 'Good morning' if h < 12 else 'Good afternoon' if h < 17 else 'Good evening'


def add_days(ad, days: int) -> str:
    return (date.fromisoformat(iso_of(ad)) + timedelta(days=days)).isoformat()


# ---------------------------------------------------------------- numbers

def _round_half_up(x: float) -> int:
    """Math.round: halves go up (towards +infinity)."""
    return math.floor(x + 0.5)


def group_digits(int_part: str, style: str = 'lakh') -> str:
    """Group with lakh/crore (12,34,567) or international (1,234,567) separators."""
    if style == 'intl':
        return re.sub(r'\B(?=(\d{3})+(?!\d))', ',', int_part)
    if len(int_part) <= 3:
        return int_part
    rest = re.sub(r'\B(?=(\d{2})+(?!\d))', ',', int_part[:-3])
    return f'{rest},{int_part[-3:]}'


def fmt_money(value, *, style: str = 'lakh', currency: bool = True, decimals: int = 2) -> str:
    """Money from a Decimal, decimal string or number, without float drift: 'NPR 12,34,567.00'."""
    if value is None or value == '':
        return DASH
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    q = d.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP) if decimals > 0 else d.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    n = f'{q:.{decimals}f}'
    neg = n.startswith('-')
    i, _, f = n.lstrip('-').partition('.')
    s = group_digits(i or '0', style) + (f'.{f}' if decimals > 0 else '')
    if neg and Decimal(n) == 0:
        neg = False
    return f'{MINUS if neg else ""}{"NPR " if currency else ""}{s}'


def fmt_num(value) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f'{value:,}'
    if isinstance(value, float):
        return f'{value:,.3f}'.rstrip('0').rstrip('.') if not value.is_integer() else f'{int(value):,}'
    if isinstance(value, Decimal):
        return fmt_num(int(value)) if value == value.to_integral_value() else f'{value:,}'
    return str(value)


def minutes_to_hm(minutes) -> str:
    """503 -> "8 h 23 m"; 0 -> "0 m"."""
    if minutes is None:
        return DASH
    minutes = int(minutes)
    h, m = divmod(minutes, 60)
    if h > 0:
        return f'{h} h {m} m' if m > 0 else f'{h} h'
    return f'{m} m'


def pct(n) -> str:
    return DASH if n is None else f'{_round_half_up(float(n))}%'


def truncate(s: str | None, n: int = 40) -> str:
    if not s:
        return ''
    return f'{s[:n - 1]}…' if len(s) > n else s


def devanagari(s) -> str:
    return to_devanagari_numerals(str(s))


def to_fixed(n: float, digits: int) -> str:
    """JavaScript Number.prototype.toFixed: rounds the exact binary value half away from zero."""
    q = Decimal(float(n)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    return f'{q:.{digits}f}'


def compact(n: float, money: bool = False) -> str:
    """Compact axis numbers: 1.2K, 3.4L (lakh) and 1.1Cr (crore) for rupee amounts."""
    a = abs(n)
    if money:
        if a >= 1e7:
            return f'{to_fixed(n / 1e7, 0 if a >= 1e8 else 1)}Cr'
        if a >= 1e5:
            return f'{to_fixed(n / 1e5, 0 if a >= 1e6 else 1)}L'
        if a >= 1e3:
            return f'{to_fixed(n / 1e3, 0)}K'
        return str(_round_half_up(n))
    if a >= 1e6:
        return f'{to_fixed(n / 1e6, 1)}M'
    if a >= 1e4:
        return f'{to_fixed(n / 1e3, 0)}K'
    return str(int(n)) if float(n).is_integer() else to_fixed(n, 1)


def nice_scale(mx: float) -> tuple[float, list[float]]:
    """A "nice" axis maximum and evenly spaced ticks from zero."""
    if not (mx > 0):
        return 1, [0, 1]
    raw = mx / 4
    pw = 10 ** math.floor(math.log10(raw))
    step = next((s for s in (m * pw for m in (1, 2, 2.5, 5, 10)) if s >= raw), raw)
    top = math.ceil(mx / step) * step
    ticks, t = [], 0.0
    while t <= top + step / 2:
        ticks.append(round(t * 1000) / 1000)
        t += step
    return top, ticks


# ---------------------------------------------------------------- BS month grid (lib/bs.ts)

@dataclass(frozen=True)
class BsCell:
    ad: str
    bs: BsDate
    weekday: int
    saturday: bool
    in_month: bool


def shift_ad(ad: str, days: int) -> str:
    return (date.fromisoformat(ad) + timedelta(days=days)).isoformat()


def safe(ad: str) -> BsDate | None:
    try:
        return ad_to_bs(ad)
    except (ValueError, TypeError):
        return None


def bs_month_grid(year: int, month: int) -> list[BsCell]:
    """Cells for a BS month grid, padded to full weeks starting Sunday (Saturday is the weekly off)."""
    days = days_in_bs_month(year, month)
    cells: list[BsCell] = []
    first_ad = bs_to_ad(BsDate(year, month, 1))
    lead = weekday_of(first_ad)
    for i in range(lead, 0, -1):
        ad = shift_ad(first_ad, -i)
        bs = safe(ad)
        if bs:
            cells.append(BsCell(ad, bs, weekday_of(ad), weekday_of(ad) == 6, False))
    for day in range(1, days + 1):
        ad = bs_to_ad(BsDate(year, month, day))
        cells.append(BsCell(ad, BsDate(year, month, day), weekday_of(ad), weekday_of(ad) == 6, True))
    while len(cells) % 7 != 0:
        ad = shift_ad(cells[-1].ad, 1)
        bs = safe(ad)
        if not bs:
            break
        cells.append(BsCell(ad, bs, weekday_of(ad), weekday_of(ad) == 6, False))
    return cells


def in_range(y: int) -> bool:
    r = supported_bs_years()
    return r['min'] <= y <= r['max']


def prev_month(y: int, m: int) -> dict[str, int]:
    """Previous month, clamped to the supported calendar (the first supported month stays put)."""
    nxt = {'year': y - 1, 'month': 12} if m == 1 else {'year': y, 'month': m - 1}
    return nxt if in_range(nxt['year']) else {'year': y, 'month': m}


def next_month(y: int, m: int) -> dict[str, int]:
    nxt = {'year': y + 1, 'month': 1} if m == 12 else {'year': y, 'month': m + 1}
    return nxt if in_range(nxt['year']) else {'year': y, 'month': m}


_TYPED = re.compile(r'^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*$')


def parse_typed(text: str, calendar: str) -> str | None:
    """Parse typed input in either calendar ('bs' or 'ad'); returns the AD ISO date or None."""
    m = _TYPED.match(text or '')
    if not m:
        return None
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    try:
        if calendar == 'bs':
            return bs_to_ad(BsDate(y, mo, d))
        return date(y, mo, d).isoformat()
    except (ValueError, TypeError):
        return None


def ad_span_label(first_ad: str, last_ad: str) -> str:
    """"Sep–Oct 2026" for the Gregorian span of a BS month."""
    am, bm = AD_MONTHS[int(first_ad[5:7]) - 1], AD_MONTHS[int(last_ad[5:7]) - 1]
    if first_ad[:4] == last_ad[:4]:
        return f'{am}–{bm} {first_ad[:4]}'
    return f'{am} {first_ad[:4]}–{bm} {last_ad[:4]}'
