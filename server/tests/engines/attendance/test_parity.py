"""Parity with the TS engine: every input dumped by packages/attendance-engine/src/dump-parity.ts must give the same
output, byte for byte once serialised, including inputs_hash. Regenerate the fixtures with
  cd packages/attendance-engine && ../../apps/api/node_modules/.bin/tsx src/dump-parity.ts ../../server/tests/parity/fixtures
"""
import json
from pathlib import Path

import pytest

from engines.attendance import (
    AttendancePolicy, AttendanceRangeError, ComputeDayInput, canonical_json, compute_day, local_date_of,
    local_midnight_ms, locale_compare, minutes_between, parse_hhmm, round_minutes, sha256_hex,
)

FIXTURES = Path(__file__).parent.parent.parent / 'parity' / 'fixtures'
COMPUTE = json.loads((FIXTURES / 'attendance_compute.json').read_text(encoding='utf-8'))
MISC = json.loads((FIXTURES / 'attendance_misc.json').read_text(encoding='utf-8'))
POLICIES = COMPUTE['policies']


def _input(case: dict) -> dict:
    return {**case['input'], 'policy': POLICIES[case['input']['policy']]}


def test_fixture_is_varied():
    cases = COMPUTE['cases']
    assert len(cases) >= 3000
    outs = [c['output'] for c in cases if 'output' in c]
    statuses = {o['status'] for o in outs}
    assert statuses == {'OffShift', 'Holiday', 'WeeklyOff', 'OnLeave', 'Present', 'HalfDay', 'HalfDayLeave', 'Absent',
                        'Incomplete', 'WFH'}
    kinds = {f['kind'] for o in outs for f in o['flags']}
    assert kinds >= {'Late', 'EarlyOut', 'HolidayWork', 'OTCapExceeded', 'OTPreapprovalMissing', 'MissingOut',
                     'HalfDayLeave', 'ShortLeave', 'AutoClockOut', 'UnpairedPunch'}
    assert any(o['ot']['minutes'] > 0 for o in outs) and any(o['ot']['nightMinutes'] > 0 for o in outs)
    assert any(c['input']['shift'] and c['input']['shift']['end'] <= c['input']['shift']['start'] for c in cases)
    assert any('error' in c for c in cases)


def test_compute_day_matches_every_case():
    mismatches = []
    for i, case in enumerate(COMPUTE['cases']):
        inp = ComputeDayInput.from_dict(_input(case))
        try:
            got = compute_day(inp).to_dict()
        except AttendanceRangeError:
            got = {'error': 'RangeError'}
        want = {'error': case['error']} if 'error' in case else case['output']
        # Compare serialised forms: JS cannot tell 2 from 2.0, and this is also how the result is stored.
        if canonical_json(got) != canonical_json(want):
            mismatches.append((i, got, want))
    assert not mismatches, f'{len(mismatches)} mismatches, first: {mismatches[0]}'


def test_shift_and_policy_round_trip_to_the_same_json():
    for case in COMPUTE['cases'][:500]:
        raw = _input(case)
        assert canonical_json(ComputeDayInput.from_dict(raw).to_dict()) == canonical_json(raw)
    for p in POLICIES:
        assert AttendancePolicy.from_dict(p).to_dict() == p


@pytest.mark.parametrize('i', range(len(MISC['hashes'])))
def test_canonical_json_and_sha256(i):
    h = MISC['hashes'][i]
    assert canonical_json(h['value']) == h['json']
    assert sha256_hex(h['json']) == h['sha256']


def test_canonical_json_special_numbers():
    # JSON.stringify output for values a JSON fixture cannot carry.
    assert canonical_json([-0.0, float('nan'), float('inf'), float('-inf'), 2.0, 2 ** 64]) == \
        '[0,null,null,null,2,18446744073709552000]'


def test_locale_compare_ascii():
    bad = [(c['a'], c['b'], c['cmp'], locale_compare(c['a'], c['b'])) for c in MISC['collation']
           if locale_compare(c['a'], c['b']) != c['cmp']]
    assert not bad, bad[:5]


def test_time_helpers():
    t = MISC['time']
    for c in t['localDateOf']:
        assert local_date_of(c['ms']) == c['date'], c
    for c in t['localMidnightMs']:
        if 'error' in c:
            with pytest.raises(AttendanceRangeError):
                local_midnight_ms(c['d'])
        else:
            assert local_midnight_ms(c['d']) == c['ms'], c
    for c in t['parseHHMM']:
        if 'error' in c:
            with pytest.raises(AttendanceRangeError):
                parse_hhmm(c['s'])
        else:
            assert parse_hhmm(c['s']) == c['min'], c
    for c in t['roundMinutes']:
        assert canonical_json(round_minutes(c['min'], c['step'], c['direction'])) == canonical_json(c['out']), c
    for c in t['minutesBetween']:
        assert minutes_between(c['a'], c['b']) == c['out'], c
