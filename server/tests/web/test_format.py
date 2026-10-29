"""Ports of apps/web/src/lib/format.test.ts, lib/bs.test.ts and api/client.test.ts (error normalisation)."""
from datetime import datetime, timedelta, timezone

from engines.bs_calendar import supported_bs_years
from hajiri.web import format as f
from hajiri.web.csv import csv_cell, to_csv


# ---------------------------------------------------------------- format.test.ts

def test_groups_digits_lakh_crore_style_and_international_style():
    assert f.group_digits('1234567') == '12,34,567'
    assert f.group_digits('123') == '123'
    assert f.group_digits('1234567', 'intl') == '1,234,567'


def test_formats_money_from_decimal_strings_without_float_drift():
    assert f.fmt_money('2163.46') == 'NPR 2,163.46'
    assert f.fmt_money('123456789.5') == 'NPR 12,34,56,789.50'
    assert f.fmt_money('-40000') == '−NPR 40,000.00'
    assert f.fmt_money(None) == '—'
    assert f.fmt_money('0.005') == 'NPR 0.01'
    assert f.fmt_money('1234.5', currency=False, decimals=0) == '1,235'


def test_renders_minutes_as_hours_and_minutes():
    assert f.minutes_to_hm(503) == '8 h 23 m'
    assert f.minutes_to_hm(60) == '1 h'
    assert f.minutes_to_hm(5) == '5 m'
    assert f.minutes_to_hm(0) == '0 m'
    assert f.minutes_to_hm(None) == '—'


def test_shows_nepal_local_time_for_a_utc_instant():
    assert f.fmt_time('2026-09-21T04:32:03.000Z') == '10:17'
    assert f.fmt_time(datetime(2026, 9, 21, 4, 32, 3, tzinfo=timezone.utc)) == '10:17'
    assert f.fmt_time(None) == '—'
    assert f.fmt_time('not a time') == '—'


def test_dual_dates_follow_the_bs_table_plan_example_f():
    d = f.dual_date('2026-08-28')
    assert d.bs == '2083-05-12'
    assert d.bs_long == '12 Bhadra 2083'
    assert d.ad == '28 Aug 2026'


def test_long_english_dates():
    assert f.fmt_date_long('2026-10-13') == 'Tue, 27 Ashwin 2083'
    assert f.fmt_date_long('2026-10-14') == 'Wed, 28 Ashwin 2083'


def test_fiscal_year_and_date_arithmetic():
    assert f.fiscal_label('2026-08-28') == '2083/84'
    assert f.fiscal_label('2026-07-16') == '2082/83'
    assert f.add_days('2026-12-31', 1) == '2027-01-01'


def test_bs_date_formats_and_fallbacks():
    assert f.fmt_date('2026-10-14') == '28 Ashwin 2083'
    assert f.fmt_date(datetime(2026, 10, 13, 20, 0, tzinfo=timezone.utc)) == '28 Ashwin 2083'  # 01:45 next day in Nepal
    assert f.fmt_date('1900-01-01') == '1 Jan 1900'   # outside the table: Gregorian
    assert f.fmt_date(None) == '—'
    assert f.bs_month_label(2083, 6) == 'Ashwin 2083'
    assert f.fmt_datetime('2026-09-21T04:32:03Z') == '5 Ashwin 2083, 10:17'


def test_relative_time():
    now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    assert f.relative_time(now - timedelta(seconds=20), at=now) == 'just now'
    assert f.relative_time(now - timedelta(minutes=5), at=now) == '5 min ago'
    assert f.relative_time(now - timedelta(hours=3), at=now) == '3 h ago'
    assert f.relative_time(now - timedelta(days=4), at=now) == '4 d ago'


def test_devanagari_numerals_and_misc():
    assert f.devanagari('2083-05-12') == '२०८३-०५-१२'
    assert f.pct(49.5) == '50%' and f.pct(None) == '—'
    assert f.truncate('abcdefghij', 5) == 'abcd…'
    assert f.compact(1_250_000, money=True) == '13L' and f.compact(12_500) == '13K' and f.compact(950) == '950'
    assert f.nice_scale(0) == (1, [0, 1])
    top, ticks = f.nice_scale(87)
    assert top == 100 and ticks == [0, 25, 50, 75, 100]


# ---------------------------------------------------------------- bs.test.ts

def test_bs_month_grid_starts_on_sunday_contains_every_day_and_marks_saturdays():
    grid = f.bs_month_grid(2083, 5)
    assert len(grid) % 7 == 0
    assert grid[0].weekday == 0
    in_month = [c for c in grid if c.in_month]
    assert len(in_month) == 31
    assert in_month[11].ad == '2026-08-28'
    assert in_month[12].saturday is True


def test_month_navigation_wraps_years():
    assert f.prev_month(2083, 1) == {'year': 2082, 'month': 12}
    assert f.next_month(2083, 12) == {'year': 2084, 'month': 1}


def test_navigation_never_leaves_the_supported_calendar():
    r = supported_bs_years()
    assert f.prev_month(r['min'], 1) == {'year': r['min'], 'month': 1}
    assert f.next_month(r['max'], 12) == {'year': r['max'], 'month': 12}
    assert f.prev_month(2083, 1) == {'year': 2082, 'month': 12}


def test_typed_fallback_accepts_both_calendars_and_rejects_impossible_dates():
    assert f.parse_typed('2083-05-12', 'bs') == '2026-08-28'
    assert f.parse_typed('2026/08/28', 'ad') == '2026-08-28'
    assert f.parse_typed('2083-13-01', 'bs') is None
    assert f.parse_typed('2026-02-30', 'ad') is None
    assert f.parse_typed('nope', 'ad') is None


def test_ad_span_label():
    assert f.ad_span_label('2026-09-17', '2026-10-17') == 'Sep–Oct 2026'
    assert f.ad_span_label('2026-12-16', '2027-01-14') == 'Dec 2026–Jan 2027'


# ---------------------------------------------------------------- client.test.ts: error normalisation (JSON errors)

def _json_error(rf, status, message, details=None):
    import json
    from hajiri.web.middleware import error_response
    req = rf.post('/x', HTTP_ACCEPT='application/json')
    req.auth_method = None
    r = error_response(req, status, message, details)
    return r.status_code, json.loads(r.content)


def test_error_bodies_keep_message_and_details(rf, settings):
    # client.test.ts 'uses the error field and keeps structured bodies as details'
    assert _json_error(rf, 409, 'already exists', {'row': 3}) == (409, {'error': 'already exists', 'details': {'row': 3}})
    assert _json_error(rf, 404, 'not found') == (404, {'error': 'not found'})
    # Database errors flatten the constraint like app.ts: {error: 'already exists', constraint}
    assert _json_error(rf, 409, 'already exists', {'constraint': 'x'}) == (409, {'error': 'already exists', 'constraint': 'x'})


def test_validation_issues_are_joined_with_their_paths():
    # client.test.ts 'joins validation issues with their paths'
    from hajiri.core.errors import ValidationError
    from hajiri.web.middleware import error_message
    e = ValidationError([{'path': 'from', 'message': 'YYYY-MM-DD'}, {'path': '', 'message': 'bad'}])
    assert error_message(e) == 'from: YYYY-MM-DD; bad' and len(e.issues) == 2
    from hajiri.core.errors import BadRequestError
    assert error_message(BadRequestError('not enough', {'suggestion': {'paidDays': 1, 'lwpDays': 2}})) == 'not enough'


def test_the_leave_split_suggestion_reaches_api_clients(rf):
    # client.test.ts 'surfaces the leave split suggestion'
    status, body = _json_error(rf, 400, 'not enough', {'suggestion': {'paidDays': 1, 'lwpDays': 2}})
    assert body['details']['suggestion'] == {'paidDays': 1, 'lwpDays': 2}


# ---------------------------------------------------------------- CSV (reports/csv.ts)

def test_csv_cells_quote_and_neutralise_formulas():
    assert csv_cell(None) == ''
    assert csv_cell('plain') == 'plain'
    assert csv_cell('a,b') == '"a,b"'
    assert csv_cell('say "hi"') == '"say ""hi"""'
    assert csv_cell('=SUM(A1)') == "'=SUM(A1)"
    assert csv_cell('-12.5') == '-12.5'
    assert csv_cell('-x') == "'-x"
    assert csv_cell(True) == 'true'
    assert csv_cell(datetime(2026, 9, 21, 4, 32, 3, tzinfo=timezone.utc)) == '2026-09-21T04:32:03.000Z'


def test_to_csv_has_bom_and_crlf():
    out = to_csv([{'key': 'a', 'header': 'A'}, {'key': 'b', 'header': 'B'}], [{'a': 1, 'b': 'राम'}])
    assert out == '﻿A,B\r\n1,राम\r\n'
