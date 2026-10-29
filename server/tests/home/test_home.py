"""Home (HomePage.tsx and App.tsx Home): who lands where, the clock-in card and its punch flow, the month and requests."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from django.test import Client

from tests.support.sql import execute, one
from hajiri.home import services as s
from tests.web.factories import make_employee, signed_in_client


def client_for(login) -> Client:
    c = Client()
    c.cookies['hajiri_session'] = login.token
    return c


@pytest.fixture
def sita(demo, login_as):
    return client_for(login_as(['employee'], demo['sita']))


def test_describe_request_and_worked_minutes():
    assert s.describe_request({'kind': 'regularisation', 'detail': '2026-10-15 wfh'}) == {'title': 'Work from home', 'detail': 'Thu, 29 Ashwin 2083'}
    assert s.describe_request({'kind': 'leave', 'detail': '2026-10-15 to 2026-10-17'})['detail'] == 'Thu, 29 Ashwin 2083 to Sat, 31 Ashwin 2083'
    assert s.describe_request({'kind': 'leave', 'detail': '2026-10-15 to 2026-10-15'})['detail'] == 'Thu, 29 Ashwin 2083'
    assert s.describe_request({'kind': 'ot', 'detail': '2026-10-15 120 min'}) == {'title': 'Overtime', 'detail': 'Thu, 29 Ashwin 2083, 2 h'}
    assert s.describe_request({'kind': 'shift_swap', 'detail': 'x'}) == {'title': 'Shift swap', 'detail': 'x'}
    t0 = datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)
    ps = [{'punched_at': t0, 'direction': 'in'}, {'punched_at': t0 + timedelta(minutes=90), 'direction': 'out'},
          {'punched_at': t0 + timedelta(minutes=100), 'direction': 'in'}]
    assert s.worked_from_punches(ps, (t0 + timedelta(minutes=130)).timestamp() * 1000) == 120
    assert s.next_direction([]) == 'in' and s.next_direction(ps[:1]) == 'out' and s.next_direction(ps[:2]) == 'in'
    assert s.month_stats({'byStatus': {'Present': 3, 'WFH': 1, 'Absent': 2}, 'lateMin': 20, 'otMin': 0})[0]['value'] == 4
    st = s.month_stats({'byStatus': {'Absent': 2}, 'lateMin': 20, 'otMin': 0})
    assert st[1]['tone'] == 'warn' and st[3]['tone'] == 'bad' and 'tone' not in st[2]


def test_working_today_hides_missing_out_flags():
    from hajiri.core.clock import npt_today
    d = npt_today().isoformat()
    day = {'status': 'Incomplete', 'first_in': '2026-10-15T04:00:00Z', 'last_out': None, 'worked_min': 0, 'shift_id': None,
           'late_min': 5, 'flags': [{'kind': 'MissingOut'}, {'kind': 'Late', 'minutes': 5}]}
    card = s.today_card({'date': d, 'day': day, 'punches': [{'id': '1', 'punched_at': '2026-10-15T04:00:00Z', 'direction': 'in',
                                                             'source': 'web', 'remote': False}]}, None, [])
    assert card['status'] == 'Working' and card['flags'] == ['Late by 5 min'] and card['clocked_in']
