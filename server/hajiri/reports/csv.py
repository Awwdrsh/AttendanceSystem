"""
CSV for Excel (port of apps/api/src/modules/reports/csv.ts and sendTable in reports/routes.ts), written by Python's csv
module through hajiri.web.csv. Every module's CSV export goes through csv_cell, which neutralises spreadsheet formulas.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from django.http import HttpResponse

from hajiri.core.bsdate import bs_label
from hajiri.core.numbers import to_fixed
from hajiri.core.clock import NPT
from hajiri.web import csv as web_csv


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


def _text(v: Any) -> Any:
    """A report value as CSV text: booleans true/false, instants ISO UTC with milliseconds, whole floats without .0,
    JSON values compact (what the TS export wrote). None stays None (an empty cell)."""
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(',', ':'), default=str)
    if isinstance(v, (datetime, date)):
        return v                       # hajiri.web.csv writes dates ISO and instants in UTC with milliseconds
    return str(v)


def csv_cell(v: Any) -> str:
    """One RFC 4180 cell (the csv module). A text cell starting with = + - @ tab or CR would be evaluated by Excel, so
    it gets a leading apostrophe; plain numbers (including negatives) are left alone."""
    return web_csv.csv_cell(_text(v))


def to_csv(columns: Iterable[Column | dict], rows: Iterable[dict]) -> str:
    """CSV with a UTF-8 BOM so Excel opens Devanagari correctly, CRLF line ends."""
    cs = [c.to_dict() if isinstance(c, Column) else c for c in columns]
    return web_csv.to_csv(cs, ({c['key']: _text(r.get(c['key'])) for c in cs} for r in rows))


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
    return d.astimezone(NPT).strftime('%H:%M')


def hours(minutes) -> str:
    """Minutes as hours with two decimals ('0.00' for none)."""
    if not minutes:
        return '0.00'
    return to_fixed(float(minutes) / 60, 2)


