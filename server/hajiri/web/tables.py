"""
Server-side DataTable (components/DataTable.tsx): columns, search, sorting, pagination and CSV export, re-rendered
in place with HTMX.

    table = Table(request, rows, [
        Column('emp_code', 'Code', sortable=True, mono=True),
        Column('name_en', 'Name', sortable=True, link=lambda r: f"/employees/{r['id']}"),
        Column('join_date', 'Joined', format='bs_date', sortable=True),
        Column('status', 'Status', format='status'),
        Column('salary', 'Salary', format='npr', align='right'),
    ], id='employees', search=('emp_code', 'name_en'), default_sort='emp_code', export='employees')
    return table_response(request, table, 'employees/list.html', {'table': table})

and in the template `{% data_table table %}`. Rows are dicts, objects or a QuerySet. Query parameters are prefixed
with the table id so two tables on one page do not clash: ?<id>-q=, ?<id>-sort=, ?<id>-dir=, ?<id>-page=, and
?<id>-export=csv.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from django.db.models import Q, QuerySet
from django.http import HttpResponse
from django.shortcuts import render
from django.utils.html import format_html

from . import format as fmt
from .csv import csv_response
from .htmx import htmx_target


def get_value(row: Any, key: str) -> Any:
    """row['a'] for dicts, row.a for objects; 'a.b' follows relations."""
    cur = row
    for part in key.split('.'):
        if cur is None:
            return None
        cur = cur.get(part) if isinstance(cur, dict) else getattr(cur, part, None)
    return cur() if callable(cur) and not isinstance(cur, type) else cur


# Named cell formats: the same rules as the template filters.
FORMATS: dict[str, Callable[[Any], str]] = {
    'bs_date': fmt.fmt_date,
    'bs_date_long': fmt.fmt_date_long,
    'bs_date_short': fmt.fmt_date_short,
    'bs_datetime': fmt.fmt_datetime,
    'time': fmt.fmt_time,
    'relative': fmt.relative_time,
    'npr': fmt.fmt_money,
    'money': lambda v: fmt.fmt_money(v, currency=False),
    'num': lambda v: fmt.DASH if v is None else fmt.fmt_num(v),
    'minutes': fmt.minutes_to_hm,
    'pct': fmt.pct,
    'yesno': lambda v: fmt.DASH if v is None else ('Yes' if v else 'No'),
}


@dataclass
class Column:
    key: str
    header: str
    sortable: bool = False
    align: str = 'left'                       # left | right | center
    format: str | Callable[[Any], Any] | None = None  # a FORMATS name, 'status', or a function of the value
    render: Callable[[Any], Any] | None = None  # function of the whole row returning text or safe HTML
    template: str | None = None               # template rendered with {'row': row, 'value': value}
    link: Callable[[Any], str | None] | None = None  # cell links to this URL
    csv: Callable[[Any], Any] | bool = True   # value for CSV export (function of the row), False to leave out
    sort_key: Callable[[Any], Any] | None = None  # for in-memory rows; QuerySets order by `order_by` or `key`
    order_by: str | None = None
    mono: bool = False
    muted: bool = False
    width: str | None = None
    sticky: bool = False
    hide_below: str | None = None             # 'sm' | 'md': hidden on narrow screens

    def __post_init__(self):
        if self.format in ('npr', 'money', 'num', 'minutes', 'pct') and self.align == 'left':
            self.align = 'right'


@dataclass
class Cell:
    column: Column
    value: Any
    text: Any
    url: str | None


@dataclass
class Row:
    obj: Any
    key: str
    cells: list[Cell]
    url: str | None = None
    highlight: bool = False


@dataclass
class Table:
    request: Any
    rows: Iterable[Any] | QuerySet
    columns: Sequence[Column]
    id: str = 'table'
    search: Sequence[str] = ()                # keys searched by the box (icontains); empty = no search box
    search_placeholder: str = 'Search'
    default_sort: str | None = None           # column key, '-key' for descending
    page_size: int | None = 25                # None = no pagination
    export: str | None = None                 # CSV filename (no extension) to offer an export link
    empty_title: str = 'Nothing here yet.'
    empty_hint: str | None = None
    empty_search_title: str = 'Nothing matches that search.'   # shown instead when a search finds nothing
    empty_search_hint: str | None = 'Try fewer or different words.'
    row_key: str = 'id'
    row_url: Callable[[Any], str | None] | None = None
    highlight: Callable[[Any], bool] | None = None
    selectable: bool = False                  # a checkbox per row named "<id>-selected" holding the row key
    bulk_actions: str | None = None           # template rendered in the bulk bar when rows are selected
    max_height: str | None = None
    extra_params: dict = field(default_factory=dict)  # kept on sort/page links (filters set by the page)

    def __post_init__(self):
        g = self.request.GET
        self.q = (g.get(self.p('q')) or '').strip()[:100]
        sort = g.get(self.p('sort')) or ''
        keys = {c.key for c in self.columns if c.sortable}
        if sort not in keys:
            sort = ''
        d = g.get(self.p('dir'))
        if not sort and self.default_sort:
            sort, d = self.default_sort.lstrip('-'), 'desc' if self.default_sort.startswith('-') else 'asc'
        self.sort, self.dir = sort, ('desc' if d == 'desc' else 'asc')
        try:
            self.page = max(1, int(g.get(self.p('page')) or 1))
        except ValueError:
            self.page = 1
        self._filtered = None

    # ------------------------------------------------------------ query
    def p(self, name: str) -> str:
        return f'{self.id}-{name}'

    def _col(self, key: str) -> Column | None:
        return next((c for c in self.columns if c.key == key), None)

    def filtered(self):
        if self._filtered is not None:
            return self._filtered
        data = self.rows
        if isinstance(data, QuerySet):
            if self.q and self.search:
                cond = Q()
                for k in self.search:
                    cond |= Q(**{f'{k.replace(".", "__")}__icontains': self.q})
                data = data.filter(cond)
            if self.sort:
                c = self._col(self.sort)
                ob = (c.order_by if c and c.order_by else self.sort).replace('.', '__')
                data = data.order_by(f'-{ob}' if self.dir == 'desc' else ob)
        else:
            data = list(data)
            if self.q and self.search:
                needle = self.q.casefold()
                data = [r for r in data if any(needle in str(get_value(r, k) or '').casefold() for k in self.search)]
            if self.sort:
                c = self._col(self.sort)
                keyf = (c.sort_key if c and c.sort_key else (lambda r, k=self.sort: get_value(r, k)))
                present = [r for r in data if keyf(r) is not None]
                missing = [r for r in data if keyf(r) is None]
                present.sort(key=lambda r: _sortable(keyf(r)), reverse=self.dir == 'desc')
                data = present + missing      # empty values always last
        self._filtered = data
        return data

    @property
    def total(self) -> int:
        data = self.filtered()
        return data.count() if isinstance(data, QuerySet) else len(data)

    @property
    def num_pages(self) -> int:
        return 1 if not self.page_size else max(1, math.ceil(self.total / self.page_size))

    def page_rows(self) -> list:
        data = self.filtered()
        if not self.page_size:
            return list(data)
        page = min(self.page, self.num_pages)
        start = (page - 1) * self.page_size
        return list(data[start:start + self.page_size])

    # ------------------------------------------------------------ rendering
    def cell(self, row: Any, c: Column) -> Cell:
        from django.template.loader import render_to_string
        value = get_value(row, c.key)
        if c.render:
            text = c.render(row)
        elif c.template:
            text = render_to_string(c.template, {'row': row, 'value': value, 'request': self.request})
        elif c.format == 'status':
            from .templatetags.hj import status_badge_html
            text = status_badge_html(value)
        elif callable(c.format):
            text = c.format(value)
        elif isinstance(c.format, str):
            text = FORMATS[c.format](value)
        else:
            text = fmt.DASH if value is None or value == '' else value
        return Cell(c, value, text, c.link(row) if c.link else None)

    def body(self) -> list[Row]:
        out = []
        for r in self.page_rows():
            out.append(Row(r, str(get_value(r, self.row_key)), [self.cell(r, c) for c in self.columns],
                           self.row_url(r) if self.row_url else None, bool(self.highlight and self.highlight(r))))
        return out

    def url(self, **changes) -> str:
        """This page's URL with this table's parameters changed (None removes one)."""
        params = self.request.GET.copy()
        for k, v in self.extra_params.items():
            params.setdefault(k, v)
        for k, v in changes.items():
            name = self.p(k)
            if v is None or v == '':
                params.pop(name, None)
            else:
                params[name] = str(v)
        params.pop(self.p('export'), None)
        qs = params.urlencode()
        return f'{self.request.path}?{qs}' if qs else self.request.path

    def sort_url(self, key: str) -> str:
        d = 'desc' if self.sort == key and self.dir == 'asc' else 'asc'
        return self.url(sort=key, dir=d, page=None)

    def headers(self) -> list[dict]:
        out = []
        for c in self.columns:
            active = c.sortable and self.sort == c.key
            out.append({'column': c, 'url': self.sort_url(c.key) if c.sortable else None, 'active': active,
                        'dir': self.dir if active else None,
                        'aria': ('descending' if self.dir == 'desc' else 'ascending') if active else 'none'})
        return out

    @property
    def export_url(self) -> str | None:
        if not self.export:
            return None
        params = self.request.GET.copy()
        params[self.p('export')] = 'csv'
        return f'{self.request.path}?{params.urlencode()}'

    def pages(self) -> dict:
        n, cur = self.num_pages, min(self.page, self.num_pages)
        first = (cur - 1) * (self.page_size or 0) + 1 if self.total else 0
        last = min(self.total, cur * self.page_size) if self.page_size else self.total
        return {'current': cur, 'count': n, 'first': first, 'last': last, 'total': self.total,
                'prev': self.url(page=cur - 1 if cur > 2 else None) if cur > 1 else None,
                'next': self.url(page=cur + 1) if cur < n else None}

    @property
    def wants_csv(self) -> bool:
        return bool(self.export) and self.request.GET.get(self.p('export')) == 'csv'

    def csv_response(self) -> HttpResponse:
        cols = [c for c in self.columns if c.csv is not False]
        rows = []
        for r in self.filtered():
            rows.append({c.key: (c.csv(r) if callable(c.csv) else get_value(r, c.key)) for c in cols})
        return csv_response(self.export or self.id, [{'key': c.key, 'header': c.header} for c in cols], rows)

    @property
    def is_partial(self) -> bool:
        """The request is HTMX reloading just this table."""
        return htmx_target(self.request) == self.id


def _sortable(v: Any):
    if isinstance(v, str):
        return (0, v.casefold())
    if isinstance(v, bool):
        return (1, int(v))
    try:
        return (1, float(v))
    except (TypeError, ValueError):
        return (2, str(v))


def table_response(request, table: Table, template: str, context: dict | None = None, *, status: int = 200) -> HttpResponse:
    """CSV when the export link was used, just the table for an HTMX reload of it, otherwise the full page."""
    if table.wants_csv:
        return table.csv_response()
    if table.is_partial:
        return render(request, 'components/data_table.html', {'table': table}, status=status)
    return render(request, template, {**(context or {}), 'table': context.get('table', table) if context else table},
                  status=status)


def link(url: str | None, text: Any) -> str:
    return format_html('<a href="{}">{}</a>', url, text) if url else text
