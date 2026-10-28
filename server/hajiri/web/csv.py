"""CSV export: RFC 4180 (Python's csv module) with formula-injection protection and a UTF-8 BOM (port of
modules/reports/csv.ts)."""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from django.http import HttpResponse

_FORMULA = re.compile(r'^[=+\-@\t\r]')
_NUMBER = re.compile(r'^-?\d+(\.\d+)?$')


def _iso_instant(d: datetime) -> str:
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(timezone.utc)
    return d.strftime('%Y-%m-%dT%H:%M:%S.') + f'{d.microsecond // 1000:03d}Z'


def cell_text(v: Any) -> str:
    """The text of one cell. A text cell starting with = + - @ tab or CR gets an apostrophe; plain numbers are left
    alone."""
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
    return s


def csv_cell(v: Any) -> str:
    """One cell as it appears in the file (quoted when it holds a comma, quote or line break)."""
    return _line([cell_text(v)]).rstrip('\r\n') if cell_text(v) else ''


def _line(cells: list[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator='\r\n').writerow(cells)
    return buf.getvalue()


def to_csv(columns: Sequence[Mapping[str, str]], rows: Iterable[Mapping[str, Any]]) -> str:
    """columns: [{'key': ..., 'header': ...}]. RFC 4180 with CRLF line ends and a BOM so Excel reads Devanagari."""
    buf = io.StringIO()
    buf.write('\ufeff')
    w = csv.writer(buf, lineterminator='\r\n')
    w.writerow([cell_text(c['header']) for c in columns])
    for r in rows:
        cells = [cell_text(r.get(c['key'])) for c in columns]
        # The csv module writes a row of one empty cell as "" (so it is not a blank line); the TS export wrote nothing.
        if len(cells) == 1 and cells[0] == '':
            buf.write('\r\n')
        else:
            w.writerow(cells)
    return buf.getvalue()


def csv_response(filename: str, columns: Sequence[Mapping[str, str]], rows: Iterable[Mapping[str, Any]]) -> HttpResponse:
    name = filename if filename.endswith('.csv') else f'{filename}.csv'
    resp = HttpResponse(to_csv(columns, rows), content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{name}"'
    return resp
