"""The list pages run a fixed number of queries however many claims and trips there are (no N+1)."""
from __future__ import annotations

from django.db import connection
from django.test.utils import CaptureQueriesContext

from hajiri.expenses import services as ex

from .helpers import days_ago, days_ahead

PAGES = (('sita', '/me/claims'), ('sita', '/me/claims/travel'), ('hr', '/expenses?status=all'),
         ('hr', '/expenses/travel?status=all'), ('ram', '/expenses?status=all'), ('ram', '/expenses/travel?status=all'),
         ('hr', '/_/expenses/claims/list?status=all'), ('hr', '/_/expenses/travel/list?status=all'))


def _add(w, n: int, start: int) -> None:
    for i in range(start, start + n):
        ex.create_claim(w.sita, {'title': f'Claim {i}', 'submit': i % 2 == 0, 'items': [
            {'expenseDate': days_ago(1), 'categoryId': w.cats['LOCAL_TRAVEL'], 'description': 'Bus', 'amount': '10'},
            {'expenseDate': days_ago(2), 'categoryId': w.cats['LOCAL_TRAVEL'], 'description': 'Taxi', 'amount': '20'}]})
        ex.request_travel(w.sita, {'destination': f'Town {i}', 'destinationCategory': 'city', 'purpose': 'Site visit',
                                   'fromDate': days_ahead(3 * i + 1), 'toDate': days_ahead(3 * i + 2), 'mode': 'bus'})


def _counts(w) -> dict[str, int]:
    out = {}
    for who, url in PAGES:
        c = w.client(getattr(w, who))
        with CaptureQueriesContext(connection) as q:
            r = c.get(url)
        assert r.status_code == 200, url
        out[f'{who} {url}'] = len(q)
    return out


def test_list_pages_do_not_query_per_row(policy_world):
    w = policy_world
    _add(w, 2, 0)
    _counts(w)  # warm the per-process caches (feature switches, settings) first
    few = _counts(w)
    _add(w, 8, 2)
    many = _counts(w)
    assert many == few
