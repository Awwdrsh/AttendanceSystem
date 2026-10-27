"""Port of packages/bs-calendar tests, plus a day-by-day parity check against the TS implementation's output."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engines.bs_calendar import (
    BsDate, BsRangeError, ad_to_bs, add_bs_days, all_supported_days, bs_month_bounds, bs_to_ad, days_in_bs_month,
    days_in_bs_year, fiscal_year, fiscal_year_of, format_bs, parse_bs, supported_bs_years, to_devanagari_numerals,
    weekday_of,
)

FIXTURE = Path(__file__).parent.parent / 'parity' / 'fixtures' / 'bs_calendar.json'


class TestAnchors:
    """Expected values from plan.md §6.10 and §10.5."""

    def test_baishakh_1(self):
        assert bs_to_ad(BsDate(2082, 1, 1)) == '2025-04-14'
        assert bs_to_ad(BsDate(2083, 1, 1)) == '2026-04-14'
        assert bs_to_ad(BsDate(2082, 12, 30)) == '2026-04-13'

    def test_fiscal_years(self):
        fy = fiscal_year(2082)
        assert (fy.label, fy.start_ad, fy.end_ad) == ('2082/83', '2025-07-17', '2026-07-16')
        fy = fiscal_year(2083)
        assert (fy.label, fy.start_ad, fy.end_ad) == ('2083/84', '2026-07-17', '2027-07-16')
        assert days_in_bs_month(2083, 3) == 32
        assert days_in_bs_month(2084, 3) == 32

    def test_shrawan_2083(self):
        assert bs_month_bounds(2083, 4) == {'startAd': '2026-07-17', 'endAd': '2026-08-16', 'days': 31}

    def test_worked_example_f(self):
        assert bs_to_ad(parse_bs('2083-05-12')) == '2026-08-28'
        assert weekday_of('2026-08-28') == 5
        assert weekday_of(bs_to_ad(parse_bs('2083-05-13'))) == 6
        assert bs_to_ad(parse_bs('2083-05-14')) == '2026-08-30'


class TestFiscalYearResolution:
    def test_bhadra_and_ashadh(self):
        assert fiscal_year_of(parse_bs('2083-05-12')).label == '2083/84'
        assert fiscal_year_of(parse_bs('2083-03-32')).label == '2082/83'

    def test_top_of_table_is_approximate(self):
        top = supported_bs_years()['max']
        fy = fiscal_year_of(parse_bs(f'{top}-05-01'))
        assert fy.start_year == top and fy.approximate
        assert fy.label == f'{top}/{str(top + 1)[-2:]}'
        assert fy.end_ad > bs_to_ad(BsDate(top, 12, 1))
        prev = fiscal_year(top - 1)
        assert not prev.approximate and prev.end_ad == bs_to_ad(BsDate(top, 3, days_in_bs_month(top, 3)))


class TestRange:
    def test_round_trip_every_day(self):
        count = 0
        for bs, ad in all_supported_days():
            assert ad_to_bs(ad) == bs
            count += 1
        r = supported_bs_years()
        assert count == sum(days_in_bs_year(y) for y in range(r['min'], r['max'] + 1))
        assert count > 33_000

    def test_rejects_outside_table(self):
        with pytest.raises(BsRangeError):
            ad_to_bs('1943-04-13')
        with pytest.raises(BsRangeError):
            bs_to_ad(BsDate(1999, 12, 1))
        with pytest.raises(BsRangeError):
            bs_to_ad(BsDate(2091, 1, 1))
        with pytest.raises(BsRangeError):
            parse_bs('2083-02-32')
        with pytest.raises(BsRangeError):
            ad_to_bs('2026-02-30')
        assert supported_bs_years() == {'min': 2000, 'max': 2090}
        assert ad_to_bs('1943-04-14') == BsDate(2000, 1, 1)

    def test_add_days_across_boundaries(self):
        assert add_bs_days(parse_bs('2082-12-30'), 1) == BsDate(2083, 1, 1)
        assert add_bs_days(parse_bs('2083-03-32'), 1) == BsDate(2083, 4, 1)

    def test_datetime_read_by_its_calendar_date(self):
        # Callers must pass the NPT date: 23:30 UTC on 28 Aug is already 29 Aug in Nepal.
        assert ad_to_bs(datetime(2026, 8, 28, 23, 30, tzinfo=timezone.utc)) == BsDate(2083, 5, 12)
        assert ad_to_bs('2026-08-29') == BsDate(2083, 5, 13)


class TestFormatting:
    def test_numerals(self):
        assert format_bs(BsDate(2083, 5, 1)) == '2083-05-01'
        assert format_bs(BsDate(2083, 5, 1), numerals='devanagari', separator='/') == '२०८३/०५/०१'
        assert to_devanagari_numerals('NPR 2,163') == 'NPR २,१६३'


class TestParityWithTypeScript:
    def test_every_day_and_fiscal_year_match(self):
        ref = json.loads(FIXTURE.read_text())
        ours = [f'{bs.year}-{bs.month}-{bs.day}={ad}' for bs, ad in all_supported_days()]
        assert ours == ref['days']
        for f in ref['fiscalYears']:
            fy = fiscal_year(f['startYear'])
            assert (fy.label, fy.start_ad, fy.end_ad, fy.approximate) == (f['label'], f['startAd'], f['endAd'], f['approximate'])


class TestCrosscheck:
    """Port of packages/bs-calendar/src/crosscheck.test.ts. The day-by-day comparison with the JS library is covered by
    the parity test above (the TS table was itself checked against the library); what remains portable is below."""

    def test_projected_years_declared(self):
        from engines.bs_calendar import PROJECTED_BS_YEARS, is_projected_bs_year
        assert supported_bs_years()['max'] in PROJECTED_BS_YEARS
        assert all(is_projected_bs_year(y) for y in PROJECTED_BS_YEARS)

    def test_matches_dates_read_from_nepali_patro(self):
        assert bs_to_ad(BsDate(2000, 1, 1)) == '1943-04-14'
        assert bs_to_ad(BsDate(2062, 1, 1)) == '2005-04-14'
        assert bs_to_ad(BsDate(2062, 1, 31)) == '2005-05-14'
        assert bs_to_ad(BsDate(2062, 2, 1)) == '2005-05-15'
        assert ad_to_bs('2026-10-14') == BsDate(2083, 6, 28)
        assert ad_to_bs('2026-10-17') == BsDate(2083, 6, 31)
        assert bs_to_ad(BsDate(2084, 1, 1)) == '2027-04-14'
