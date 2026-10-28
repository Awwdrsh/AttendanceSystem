"""Download and print responses shared by the pages and the JSON API: CSV files, printable HTML (never cached) and the
salary sheet in its three formats."""
from __future__ import annotations

from django.http import HttpResponse

from hajiri.web.csv import to_csv

from .salary_sheet import salary_sheet_csv, salary_sheet_html, sheet_filename


def html_response(html: str) -> HttpResponse:
    res = HttpResponse(html, content_type='text/html; charset=utf-8')
    res['Cache-Control'] = 'private, no-store'
    return res


def csv_download(filename: str, text: str) -> HttpResponse:
    res = HttpResponse(text, content_type='text/csv; charset=utf-8')
    res['Content-Disposition'] = f'attachment; filename="{filename}"'
    return res


def report_csv(r: dict) -> HttpResponse:
    """A compliance report ({filename, columns, rows}) as a CSV download."""
    return csv_download(f"{r['filename']}.csv", to_csv(r['columns'], r['rows']))


def sheet_response(fmt: str, sheet: dict):
    """The salary sheet: a CSV download, the printable page, or the data itself (JSON)."""
    if fmt == 'csv':
        return csv_download(sheet_filename(sheet), salary_sheet_csv(sheet))
    if fmt == 'html':
        return html_response(salary_sheet_html(sheet))
    return sheet
