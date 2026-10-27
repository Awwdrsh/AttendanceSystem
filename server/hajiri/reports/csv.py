"""
CSV for Excel (port of apps/api/src/modules/reports/csv.ts and sendTable in reports/routes.ts). Every module's CSV
export goes through csv_cell, which neutralises spreadsheet formulas.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

from django.http import HttpResponse

from hajiri.core.bsdate import bs_label


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    type: str | None = None  # 'text' | 'number' | 'date' | 'bool'; dates are shown in Bikram Sambat

    def to_dict(self) -> dict:
        d = {'key': self.key, 'header': self.header}
        if self.type:
            d['type'] = self.type
        return d


def cols(*specs) -> list[Column]:
    """cols(('emp_code', 'Emp code'), ('join_date', 'Join date', 'date'))."""
    return [s if isinstance(s, Column) else Column(*s) for s in specs]


@dataclass
class Table:
    """A report's result: columns and rows for the screen, or CSV."""
    columns: list[Column]
    rows: list[dict]
    filename: str = 'report'
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {'columns': [c.to_dict() for c in self.columns], 'rows': self.rows, **self.extra}


def _js_iso(d: datetime) -> str:
    d = d.astimezone(timezone.utc) if d.tzinfo else d
    return d.strftime('%Y-%m-%dT%H:%M:%S.') + f'{d.microsecond // 1000:03d}Z'


def _text(v: Any) -> str:
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, datetime):
        return _js_iso(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dict, list)):
        import json
        return json.dumps(v, ensure_ascii=False, separators=(',', ':'), default=str)
    return str(v)


def csv_cell(v: Any) -> str:
    """One RFC 4180 cell. A text cell starting with = + - @ tab or CR would be evaluated by Excel, so it gets a leading
    apostrophe; plain numbers (including negatives) are left alone."""
    if v is None:
        return ''
    s = _text(v)
    if re.match(r'[=+\-@\t\r]', s) and not re.fullmatch(r'-?\d+(\.\d+)?', s):
        s = "'" + s
    return '"' + s.replace('"', '""') + '"' if re.search(r'[",\r\n]', s) else s


def to_csv(columns: Iterable[Column | dict], rows: Iterable[dict]) -> str:
    """CSV with a UTF-8 BOM so Excel opens Devanagari correctly, CRLF line ends."""
    cs = [c if isinstance(c, Column) else Column(c['key'], c['header'], c.get('type')) for c in columns]
    lines = [','.join(csv_cell(c.header) for c in cs)]
    for r in rows:
        lines.append(','.join(csv_cell(r.get(c.key)) for c in cs))
    return '﻿' + '\r\n'.join(lines) + '\r\n'


def csv_response(filename: str, columns, rows) -> HttpResponse:
    res = HttpResponse(to_csv(columns, rows), content_type='text/csv; charset=utf-8')
    res['Content-Disposition'] = f'attachment; filename="{filename}.csv"'
    return res


def send_report(fmt: str | None, filename: str, columns, rows):
    """CSV download when fmt == 'csv', else a Table for the page."""
    if fmt == 'csv':
        return csv_response(filename, columns, rows)
    return Table(cols(*columns), list(rows), filename)


def table_csv_rows(table: Table) -> list[dict]:
    """Rows as the CSV shows them: date columns in Bikram Sambat, yes/no columns as Yes/No."""
    date_keys = [c.key for c in table.columns if c.type == 'date']
    bool_keys = [c.key for c in table.columns if c.type == 'bool']
    out = []
    for r in table.rows:
        x = dict(r)
        for k in date_keys:
            x[k] = bs_label(x[k]) if x.get(k) else ''
        for k in bool_keys:
            x[k] = '' if x.get(k) is None else ('Yes' if x[k] else 'No')
        out.append(x)
    return out


def table_response(table: Table) -> HttpResponse:
    """sendTable's CSV: dates in BS, Yes/No, and a file name made safe."""
    name = re.sub(r'[^a-zA-Z0-9._-]', '_', table.filename)
    return csv_response(name, table.columns, table_csv_rows(table))


def send_table(fmt: str | None, table: Table):
    return table_response(table) if fmt == 'csv' else table


def npt_hhmm(d: datetime | None) -> str:
    """'HH:MM' in Nepal time for an instant, or ''."""
    if not d:
        return ''
    return (d.astimezone(timezone.utc) + timedelta(minutes=345)).strftime('%H:%M')


def hours(minutes) -> str:
    """Minutes as hours with two decimals ('0.00' for none)."""
    if not minutes:
        return '0.00'
    return _to_fixed(float(minutes) / 60, 2)


def _to_fixed(x: float, n: int) -> str:
    """Number.prototype.toFixed: rounds the exact binary value half away from zero."""
    d = Decimal(x).quantize(Decimal(1).scaleb(-n), rounding='ROUND_HALF_UP')
    return f'{d:.{n}f}'
