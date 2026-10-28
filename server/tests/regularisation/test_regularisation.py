"""Port of apps/api/test/regularisation.test.ts and regularisation-qa.test.ts (through the JSON API, as the TS tests)."""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone

import pytest
from django.db import connection
from django.test import Client

from engines.attendance import local_time_ms

from hajiri.attendance.service import recompute_day
from tests.support.sql import execute, one, rows
from hajiri.regularisation import services
from tests.core.fixtures import bearer
from tests.core.punches import insert_punch

URL = '/api/v1/regularisation'


def nepal_today() -> date:
    # The service compares with the Nepal date (npt_today); a UTC date is a day behind between 18:15 and 24:00 UTC.
    from hajiri.core.clock import npt_today
    return npt_today()


def days_ago(n: int) -> str:
    return (nepal_today() - timedelta(days=n)).isoformat()


def workday_ago(n: int) -> str:
    """nth most recent day before today that is not a Saturday (the demo weekly off)."""
    count = 0
    for i in range(1, 60):
        d = days_ago(i)
        if date.fromisoformat(d).weekday() == 5:
            continue
        count += 1
        if count == n:
            return d
    raise AssertionError('unreachable')


def npt(d: str, hhmm: str) -> str:
    ms = local_time_ms(d, hhmm)
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def post(login, body, url=URL):
    r = Client().post(url, json.dumps(body), content_type='application/json', **bearer(login.token))
    return r, (r.json() if r.content else {})


def get(login, query=None, url=URL):
    r = Client().get(url, query or {}, **bearer(login.token))
    return r, (r.json() if r.content else {})


# ================================================================ regularisation.test.ts

def test_missed_out_punch_employee_files_manager_approves_punch_created_day_present(demo, reg):
    d = workday_ago(3)
    insert_punch(demo['sita'], d, '10:00')
    assert recompute_day(demo['sita'], d).day.status == 'Incomplete'
    r, body = post(reg['sita'], {'localDate': d, 'kind': 'missed_punch', 'punchTime': npt(d, '18:00'), 'punchDirection': 'out',
                                 'reason': 'forgot to punch out'})
    assert r.status_code == 201
    assert body['status'] == 'pending'
    _, inbox = get(reg['ram'], url='/api/v1/workflow/inbox')
    assert inbox['items'][0]['objectType'] == 'regularisation_request'
    assert inbox['items'][0]['summary']['kind'] == 'missed_punch'
    _, ok = post(reg['ram'], {}, url=f"/api/v1/workflow/{inbox['items'][0]['instanceId']}/approve")
    assert ok['state'] == 'approved'
    rr = one('SELECT resulting_punch_id, status FROM regularisation_request WHERE id = %s', [body['id']])
    assert rr['status'] == 'approved'
    assert rr['resulting_punch_id'] is not None
    p = one('SELECT source, direction, device_sn FROM punch_event WHERE id = %s', [rr['resulting_punch_id']])
    assert p == {'source': 'regularisation', 'direction': 'out', 'device_sn': 'MANUAL-DEMO'}
    day = recompute_day(demo['sita'], d).day
    assert (day.status, day.worked_min) == ('Present', 480)


def test_hr_fixes_a_punch_on_behalf_approved_immediately_with_a_signed_action(demo, reg):
    d2 = workday_ago(4)
    r1, b1 = post(reg['hr'], {'employeeId': demo['ram'], 'localDate': d2, 'kind': 'missed_punch', 'punchTime': npt(d2, '10:00'),
                              'punchDirection': 'in', 'reason': 'device offline'})
    assert r1.status_code == 201
    assert b1['status'] == 'approved'
    _, b2 = post(reg['hr'], {'employeeId': demo['ram'], 'localDate': d2, 'kind': 'missed_punch', 'punchTime': npt(d2, '18:00'),
                             'punchDirection': 'out', 'reason': 'device offline'})
    assert b2['status'] == 'approved'
    acts = rows("""SELECT a.decision FROM approval_action a JOIN workflow_instance w ON w.id = a.instance_id
                    WHERE w.object_id = %s""", [b1['id']])
    assert acts == [{'decision': 'approve'}]
    day = recompute_day(demo['ram'], d2).day
    assert (day.status, day.worked_min) == ('Present', 480)


def test_wfh_approval_turns_an_absent_day_into_wfh(demo, reg):
    d3 = workday_ago(5)
    r, body = post(reg['sita'], {'localDate': d3, 'kind': 'wfh', 'reason': 'worked from home'})
    assert r.status_code == 201
    inst = one('SELECT workflow_instance_id FROM regularisation_request WHERE id = %s', [body['id']])
    post(reg['hr'], {}, url=f"/api/v1/workflow/{inst['workflow_instance_id']}/approve")
    assert recompute_day(demo['sita'], d3).day.status == 'WFH'


def test_enforces_the_backdate_window_future_dates_locked_days_and_the_monthly_cap(demo, reg):
    assert post(reg['sita'], {'localDate': days_ago(45), 'kind': 'wfh', 'reason': 'old'})[0].status_code == 400
    # Corrections are for days that have happened; only work from home can be planned ahead (and not too far).
    assert post(reg['sita'], {'localDate': days_ago(-2), 'kind': 'on_duty', 'reason': 'future'})[0].status_code == 400
    assert post(reg['sita'], {'localDate': days_ago(-61), 'kind': 'wfh', 'reason': 'too far'})[0].status_code == 400
    dl = workday_ago(6)
    recompute_day(demo['sita'], dl)
    execute('UPDATE attendance_day SET locked_at = now() WHERE employee_id = %s AND local_date = %s', [demo['sita'], dl])
    assert post(reg['sita'], {'localDate': dl, 'kind': 'wfh', 'reason': 'locked'})[0].status_code == 409
    execute("""UPDATE company_setting SET value = value || '{"regularisationMonthlyCap": 1}'
                WHERE company_id = %s AND key = 'attendance.policy'""", [demo['company_id']])
    # (The TS test relied on Sita's earlier corrections this month; here one is filed first.)
    assert post(reg['sita'], {'localDate': workday_ago(1), 'kind': 'half_day', 'reason': 'left at lunch'})[0].status_code == 201
    capped, body = post(reg['sita'], {'localDate': workday_ago(1), 'kind': 'on_duty', 'reason': 'site visit'})
    assert capped.status_code == 400
    assert 'monthly limit' in body['error']


def test_employee_cannot_file_for_someone_else_cancel_only_while_pending(demo, reg):
    assert post(reg['sita'], {'employeeId': demo['ram'], 'localDate': workday_ago(2), 'kind': 'wfh',
                              'reason': 'not allowed'})[0].status_code == 403
    r, body = post(reg['ram'], {'localDate': workday_ago(2), 'kind': 'on_duty', 'reason': 'client site'})
    assert r.status_code == 201
    assert post(reg['ram'], {}, url=f"{URL}/{body['id']}/cancel")[0].status_code == 200
    assert post(reg['ram'], {}, url=f"{URL}/{body['id']}/cancel")[0].status_code == 409


# ================================================================ regularisation-qa.test.ts

@pytest.fixture
def qa(demo, reg, login_as):
    m = one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'E901', 'Unrelated Manager') RETURNING id",
            [demo['company_id']])
    return {**reg, 'other_mgr': login_as(['manager'], str(m['id']))}


def test_today_is_the_nepal_calendar_date_not_the_utc_date(qa, monkeypatch):
    # 19:00 UTC on 13 Oct is 00:45 NPT on 14 Oct: the Nepal date must already read 14 Oct.
    from hajiri.core import clock
    monkeypatch.setattr(clock, 'now', lambda: datetime(2026, 10, 13, 19, 0, tzinfo=timezone.utc))
    assert services.npt_today() == '2026-10-14'
    assert clock.now().date().isoformat() == '2026-10-13'
    monkeypatch.undo()
    # The route must accept the Nepal "today" as not-in-the-future at any hour of the day.
    r, _ = post(qa['sita'], {'localDate': services.npt_today(), 'kind': 'on_duty', 'reason': 'today in Nepal'})
    assert r.status_code == 201


def test_a_late_utc_evening_is_already_tomorrow_in_nepal(qa, monkeypatch):
    """The same rule at the service level with the clock frozen: the Nepal date of 19:00 UTC is not in the future."""
    from hajiri.core import clock
    real = datetime.now(timezone.utc)
    frozen = datetime(real.year, real.month, real.day, 19, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(clock, 'now', lambda: frozen)
    tomorrow_utc = (frozen.date() + timedelta(days=1)).isoformat()
    assert services.npt_today() == tomorrow_utc
    out = services.create_request(qa['sita'].user, {'localDate': tomorrow_utc, 'kind': 'on_duty', 'reason': 'today in Nepal'})
    assert out['status'] == 'pending'


def test_missed_punch_cannot_be_before_the_attendance_date_but_may_fall_on_the_next_day(demo, qa):
    d = days_ago(10)
    prev = days_ago(11)
    nxt = days_ago(9)
    nxt2 = days_ago(8)
    assert post(qa['hr'], {'employeeId': demo['ram'], 'localDate': d, 'kind': 'missed_punch', 'punchTime': npt(prev, '23:00'),
                           'punchDirection': 'in', 'reason': 'x'})[0].status_code == 400
    assert post(qa['hr'], {'employeeId': demo['ram'], 'localDate': d, 'kind': 'missed_punch', 'punchTime': npt(nxt, '06:00'),
                           'punchDirection': 'out', 'reason': 'night shift out'})[0].status_code == 201
    assert post(qa['hr'], {'employeeId': demo['ram'], 'localDate': d, 'kind': 'missed_punch', 'punchTime': npt(nxt2, '06:00'),
                           'punchDirection': 'out', 'reason': 'too far'})[0].status_code == 400


def test_punch_time_errors_carry_the_ts_messages(demo, qa):
    d = days_ago(10)
    r, body = post(qa['hr'], {'employeeId': demo['ram'], 'localDate': d, 'kind': 'missed_punch',
                              'punchTime': npt(days_ago(11), '23:00'), 'punchDirection': 'in', 'reason': 'reason'})
    assert body['error'] == 'punchTime must be on the attendance date or the following day'
    r, body = post(qa['hr'], {'employeeId': demo['ram'], 'localDate': d, 'kind': 'missed_punch', 'reason': 'reason'})
    assert r.status_code == 400 and body['error'] == 'punchTime and punchDirection are required for a missed punch'
    r, body = post(qa['sita'], {'localDate': d, 'kind': 'shift_change', 'reason': 'swap'})
    assert r.status_code == 400 and body['error'] == 'shiftId is required for a shift change'
    r, body = post(qa['sita'], {'localDate': days_ago(-2), 'kind': 'on_duty', 'reason': 'future'})
    assert body['error'] == 'cannot regularise a future date'
    r, body = post(qa['sita'], {'localDate': days_ago(-61), 'kind': 'wfh', 'reason': 'far'})
    assert body['error'] == 'work from home can be requested up to 60 days ahead'
    r, body = post(qa['sita'], {'localDate': days_ago(45), 'kind': 'wfh', 'reason': 'old'})
    assert body['error'] == 'corrections can only be requested within 30 days'
    r, body = post(qa['sita'], {'localDate': d, 'kind': 'nope', 'reason': 'x'})
    assert r.status_code == 400 and body['error'] == 'validation'
    assert {i['path'] for i in body['issues']} == {'kind', 'reason'}


def test_shift_change_must_reference_a_shift_of_the_same_company(demo, qa):
    c2 = one("INSERT INTO company (code, name_en) VALUES ('C2', 'C2') RETURNING id")
    sh = one("INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES (%s, 'X', 'X', '09:00', '17:00') RETURNING id",
             [c2['id']])
    d = days_ago(9)
    r, body = post(qa['sita'], {'localDate': d, 'kind': 'shift_change', 'shiftId': str(sh['id']), 'reason': 'swap'})
    assert r.status_code == 404 and body['error'] == 'shift not found'
    assert post(qa['sita'], {'localDate': d, 'kind': 'shift_change', 'shiftId': demo['shift_id'], 'reason': 'swap'})[0].status_code == 201


def test_a_manager_can_only_file_for_list_and_cancel_requests_of_direct_reports(demo, qa):
    d = days_ago(8)
    assert post(qa['other_mgr'], {'employeeId': demo['sita'], 'localDate': d, 'kind': 'wfh',
                                  'reason': 'not my report'})[0].status_code == 403
    ok, body = post(qa['ram'], {'employeeId': demo['sita'], 'localDate': d, 'kind': 'wfh', 'reason': 'yes'})
    assert ok.status_code == 201
    assert post(qa['other_mgr'], {}, url=f"{URL}/{body['id']}/cancel")[0].status_code == 403
    assert get(qa['other_mgr'], {'employeeId': demo['sita']})[0].status_code == 403
    assert post(qa['ram'], {}, url=f"{URL}/{body['id']}/cancel")[0].status_code == 200


def test_the_teams_today_list_shows_a_manager_only_direct_reports_and_themselves_hr_sees_everyone(demo, qa):
    """The TS test called /attendance/today; the attendance slice owns that endpoint, so its service is used."""
    from hajiri.attendance import api
    day = days_ago(8)

    def ids(r):
        return sorted(str(x['employee_id']) for x in r['rows'])
    mine = api.today(qa['ram'].user, {'date': day})
    assert ids(mine) == sorted([demo['sita'], demo['ram']])
    other = api.today(qa['other_mgr'].user, {'date': day})
    assert len(other['rows']) == 1
    everyone = api.today(qa['hr'].user, {'date': day})
    assert len(everyone['rows']) >= 3


@pytest.mark.django_db(transaction=True)
def test_approving_the_same_missed_punch_request_twice_never_creates_two_punches():
    from hajiri.company_settings.demo import DEMO, seed_demo
    from hajiri.core import auth
    seed_demo()
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [DEMO['ram'], DEMO['sita']])

    def login(roles, emp, email):
        auth.create_user(company_id=DEMO['company_id'], employee_id=emp, email=email, name=email,
                         password='correct-horse-battery-staple', roles=roles)
        return auth.login(email, 'correct-horse-battery-staple').token
    sita = login(['employee'], DEMO['sita'], 'sita-race@test.local')
    ram = login(['manager'], DEMO['ram'], 'ram-race@test.local')
    d = days_ago(7)
    r = Client().post(URL, json.dumps({'localDate': d, 'kind': 'missed_punch', 'punchTime': npt(d, '18:00'), 'punchDirection': 'out',
                                       'reason': 'forgot'}), content_type='application/json', **bearer(sita))
    assert r.status_code == 201
    inst = one('SELECT workflow_instance_id FROM regularisation_request WHERE id = %s', [r.json()['id']])['workflow_instance_id']
    statuses = []
    barrier = threading.Barrier(3)

    def run():
        try:
            barrier.wait()
            res = Client().post(f'/api/v1/workflow/{inst}/approve', '{}', content_type='application/json', **bearer(ram))
            statuses.append(res.status_code)
        finally:
            connection.close()
    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == [200, 409, 409]
    punches = rows("SELECT 1 AS x FROM punch_event WHERE employee_id = %s AND local_date = %s AND source = 'regularisation'",
                   [DEMO['sita'], d])
    assert len(punches) == 1
