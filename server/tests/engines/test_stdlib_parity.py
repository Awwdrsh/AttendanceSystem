"""The engines use standard Python (pyuca, json, datetime, bisect) wherever the recorded TS output allows; these tests
pin the edges where that standard behaviour and the JS behaviour could part, so a library change cannot drift."""
import json
from datetime import date

import pytest
from hypothesis import given, strategies as st

from engines.attendance import Punch, canonical_json, local_date_of, locale_compare, sort_punches
from engines.attendance.timeutil import _EPOCH_ORDINAL, _civil_from_days
from engines.bs_calendar import BsDate, BsRangeError, ad_to_bs, all_supported_days, bs_to_ad, parse_bs, parse_iso_date

ASCII = st.text(alphabet=st.characters(min_codepoint=0, max_codepoint=127), max_size=8)


# --- locale_compare (pyuca) -----------------------------------------------------------------------------------------

def test_locale_compare_basics_match_icu():
    assert locale_compare('a', 'B') == -1          # primary before case
    assert locale_compare('ram', 'Ram') == -1      # lowercase first at the tertiary level
    assert locale_compare('a\x01b', 'ab') == 0     # C0 controls are ignorable, and there is no code-point tie-break
    assert locale_compare('-1', '1') == -1         # punctuation is not ignorable and sorts before digits
    assert locale_compare('Ángela', 'Angela') == 1 and locale_compare('Ángela', 'Bob') == -1


@given(ASCII, ASCII)
def test_locale_compare_is_antisymmetric(a, b):
    assert locale_compare(a, b) == -locale_compare(b, a)


def test_sort_punches_orders_by_time_then_collated_id():
    ps = [Punch(id=i, at=at, direction='unknown', verify_mode='fp', source='device') for i, at in
          [('b', 2), ('B', 1), ('a', 1), ('_', 1), ('A', 3)]]
    assert [p.id for p in sort_punches(ps)] == ['_', 'a', 'B', 'b', 'A']


# --- canonical_json strings (json.dumps fast path) --------------------------------------------------------------------

@given(st.text())
def test_canonical_json_strings_round_trip(s):
    out = canonical_json(s)
    assert json.loads(out) == s
    assert all(c not in out for c in '\n\r\t\x00')


def test_canonical_json_string_escapes_match_json_stringify():
    assert canonical_json('a"b\\c\n\x01\x7f é😀') == '"a\\"b\\\\c\\n\\u0001\x7f é😀"'
    # Lone surrogates take the JS path: escaped as lowercase \udxxx; a pair held as two code points is one character.
    assert canonical_json('\ud800x') == '"\\ud800x"'
    assert canonical_json('😀') == '"😀"'


# --- local_date_of (datetime with the expanded-year fallback) -------------------------------------------------------

@given(st.integers(min_value=-1_000_000, max_value=3_000_000))
def test_hinnant_fallback_agrees_with_datetime(days):
    if date.min.toordinal() <= _EPOCH_ORDINAL + days <= date.max.toordinal():
        d = date.fromordinal(_EPOCH_ORDINAL + days)
        assert _civil_from_days(days) == (d.year, d.month, d.day)


def test_local_date_of_outside_datetime_range_uses_js_expanded_years():
    day = 86_400_000
    assert local_date_of(0) == '1970-01-01'
    npt = 20_700_000
    assert local_date_of(8.64e15 - npt) == '+275760-09'        # toISOString '+275760-09-13T...'.slice(0, 10)
    assert local_date_of(253_402_300_800_000 - npt) == '+010000-01'
    assert local_date_of(-62_135_596_800_000 - npt - day) == '0000-12-31'   # the day before year 1
    assert local_date_of(-8.64e15 - npt) == '-271821-04'         # '-271821-04-20T...'.slice(0, 10)
    with pytest.raises(ValueError):
        local_date_of(8.64e15 - npt + 1)


# --- BS calendar (bisect lookup, JS-equivalent regexes) ---------------------------------------------------------------

def test_ad_to_bs_inverts_bs_to_ad_on_every_supported_day():
    for bs, ad in all_supported_days():
        assert ad_to_bs(ad) == bs
        assert bs_to_ad(bs) == ad


@pytest.mark.parametrize('s', ['२०८२-०१-०१', '2082-01-01\n', ' 2082-01-01', '2082-1-01'])
def test_parse_bs_rejects_what_the_js_regex_rejects(s):
    with pytest.raises(BsRangeError):
        parse_bs(s)


@pytest.mark.parametrize('s', ['२०२५-०४-१४', '2025-04-14\n'])
def test_parse_iso_date_rejects_what_the_js_regex_rejects(s):
    with pytest.raises(BsRangeError):
        parse_iso_date(s)


def test_parse_bs_accepts_ascii():
    assert parse_bs('2082-01-01') == BsDate(2082, 1, 1)
