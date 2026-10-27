"""CSV export: RFC 4180 cells with formula-injection protection and a UTF-8 BOM (port of modules/reports/csv.ts)."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from django.http import HttpResponse

_FORMULA = re.compile(r'^[=+\-@\t\r]')
_NUMBER = re.compile(r'^-?\d+(\.\d+)?$')
_QUOTE = re.compile(r'[",\r\n]')


def _iso_instant(d: datetime) -> str:
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(timezone.utc)
    return d.strftime('%Y-%m-%dT%H:%M:%S.') + f'{d.microsecond // 1000:03d}Z'


def csv_cell(v: Any) -> str:
    """One CSV cell. A text cell starting with = + - @ tab or CR gets an apostrophe; plain numbers are left alone."""
    if v is None:
        return ''
    if isinstance(v, bool):
        s = 'true' if v else 'false'
    elif isinstance(v, datetime):
        s = _iso_instant(v)
    elif isinstance(v, date):
        s = v.isoformat()
    else:
        s = str(v)
    if _FORMULA.match(s) and not _NUMBER.match(s):
        s = f"'{s}"
    return f'"{s.replace(chr(34), chr(34) * 2)}"' if _QUOTE.search(s) else s


def to_csv(columns: Sequence[Mapping[str, str]], rows: Iterable[Mapping[str, Any]]) -> str:
    """columns: [{'key': ..., 'header': ...}]. RFC 4180 with CRLF line ends and a BOM so Excel reads Devanagari."""
    lines = [','.join(csv_cell(c['header']) for c in columns)]
    for r in rows:
        lines.append(','.join(csv_cell(r.get(c['key'])) for c in columns))
    return '﻿' + '\r\n'.join(lines) + '\r\n'


def csv_response(filename: str, columns: Sequence[Mapping[str, str]], rows: Iterable[Mapping[str, Any]]) -> HttpResponse:
    name = filename if filename.endswith('.csv') else f'{filename}.csv'
    resp = HttpResponse(to_csv(columns, rows), content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{name}"'
    return resp
