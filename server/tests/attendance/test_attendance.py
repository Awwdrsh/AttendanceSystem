"""Port of apps/api/test/attendance.test.ts (schema guarantees and recompute) and the attendance parts of
qa-foundation.test.ts (tenancy, manager scoping, exceptions, recompute-range, half-day regularisation)."""
import threading
from datetime import datetime, timezone

import pytest
from django.db import DatabaseError, connection, transaction

from hajiri.attendance import api, repo
from hajiri.attendance.service import (
    dates_for_punch, enqueue_recompute, is_attendance_locked, list_days, lock_attendance, recompute_day, recompute_for_punch,
    recompute_queued_for,
)
from hajiri.core import auth
from hajiri.core.clock import npt_today
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import ForbiddenError, NotFoundError, ValidationError
from tests.core.fixtures import PW
from tests.core.punches import insert_punch

OTHER = '00000000-0000-7000-8000-00000000aaaa'


@pytest.fixture
def hr(login_as):
    return login_as(['hr']).user


def recompute(user, emp, d):
    return api.recompute(user, {'employeeId': emp, 'localDate': d})


# ---------------------------------------------------------------- schema guarantees

def test_punch_event_is_append_only(demo):
    insert_punch(demo['ram'], '2026-09-14', '10:00', '2')
    for sql in ("UPDATE punch_event SET verify_mode = 'face' WHERE device_user_pin = '2'", "DELETE FROM punch_event WHERE device_user_pin = '2'"):
        with pytest.raises(DatabaseError, match='append-only'), transaction.atomic():
            execute(sql)


def test_rejects_overlapping_shift_assignments(demo):
    with pytest.raises(DatabaseError, match='conflicting key value|overlap'), transaction.atomic():
        execute("INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES (%s, %s, daterange('2026-06-01', '2026-07-01'))",
                [demo['ram'], demo['shift_id']])


def test_never_queues_a_shell_command(demo):
    with pytest.raises(DatabaseError, match='device_command_no_shell'), transaction.atomic():
        execute("INSERT INTO device_command (device_id, command) VALUES (%s, 'SHELL rm -rf /')", [demo['device_id']])


def test_audit_log_is_hash_chained_and_immutable(db):
    execute("""INSERT INTO audit_log (action, table_name, row_id, after) VALUES ('create', 'employee', 'x', '{"a":1}')""")
    execute("""INSERT INTO audit_log (action, table_name, row_id, after) VALUES ('update', 'employee', 'x', '{"a":2}')""")
    r = rows("SELECT prev_hash, row_hash FROM audit_log WHERE row_id = 'x' ORDER BY id")
    assert r[1]['prev_hash'] == r[0]['row_hash']
    with pytest.raises(DatabaseError, match='append-only'), transaction.atomic():
        execute("UPDATE audit_log SET action = 'x'")


def test_bs_calendar_is_seeded(demo):
    assert one("SELECT bs_date FROM bs_calendar WHERE ad_date = '2026-08-28'")['bs_date'] == '2083-05-12'


# ---------------------------------------------------------------- recompute

def test_worked_example_a_from_stored_punches_stores_one_version(demo, hr):
    insert_punch(demo['sita'], '2026-09-21', '10:17')
    insert_punch(demo['sita'], '2026-09-21', '18:40')
    first = recompute(hr, demo['sita'], '2026-09-21')
    assert (first['changed'], first['version']) == (True, 1)
    d = first['day']
    assert (d['status'], d['workedMin'], d['lateMin'], d['flags']) == ('Present', 503, 2, [{'kind': 'Late', 'minutes': 2}])
    again = recompute(hr, demo['sita'], '2026-09-21')
    assert (again['changed'], again['version']) == (False, 1)
    insert_punch(demo['sita'], '2026-09-21', '19:00')
    third = recompute(hr, demo['sita'], '2026-09-21')
    assert third['version'] == 2
    assert any(f['kind'] == 'UnpairedPunch' and isinstance(f.get('punchId'), str) for f in third['day']['flags'])
    days = api.days(hr, {'employeeId': demo['sita'], 'from': '2026-09-21', 'to': '2026-09-21'})
    assert len(days) == 1
    assert (days[0]['version'], days[0]['bs_date'], days[0]['status']) == (2, '2083-06-05', 'Present')


@pytest.mark.django_db(transaction=True)
def test_concurrent_recomputes_never_collide_on_the_version_number():
    from hajiri.company_settings.demo import DEMO, seed_demo
    seed_demo()
    insert_punch(DEMO['ram'], '2026-09-15', '10:00', '2')
    insert_punch(DEMO['ram'], '2026-09-15', '18:00', '2')
    errors = []

    def run():
        try:
            recompute_day(DEMO['ram'], '2026-09-15')
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            connection.close()
    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert one("SELECT count(*)::int AS n FROM attendance_day WHERE employee_id = %s AND local_date = '2026-09-15'", [DEMO['ram']])['n'] == 1


def test_weekly_off_holiday_and_approved_leave(demo, hr):
    assert recompute(hr, demo['sita'], '2026-09-26')['day']['status'] == 'WeeklyOff'
    execute("INSERT INTO holiday (company_id, local_date, name_en) VALUES (%s, '2026-10-19', 'Dashain')", [demo['company_id']])
    insert_punch(demo['sita'], '2026-10-19', '10:00')
    insert_punch(demo['sita'], '2026-10-19', '14:00')
    hol = recompute(hr, demo['sita'], '2026-10-19')
    assert hol['day']['status'] == 'Holiday'
    assert {'kind': 'HolidayWork', 'workedMin': 240} in hol['day']['flags']
    lt = one("SELECT id FROM leave_type WHERE company_id = %s AND code = 'LWP'", [demo['company_id']])
    execute("""INSERT INTO leave_request (employee_id, leave_type_id, from_date, to_date, days_requested, status, decided_at)
               VALUES (%s, %s, '2026-09-23', '2026-09-23', 1, 'approved', now())""", [demo['sita'], lt['id']])
    leave = recompute(hr, demo['sita'], '2026-09-23')
    assert leave['day']['status'] == 'OnLeave'
    assert {'kind': 'UnpaidLeave'} in leave['day']['flags']
    stored = one("SELECT flags FROM v_attendance_latest WHERE employee_id = %s AND local_date = '2026-09-23'", [demo['sita']])
    assert {'kind': 'UnpaidLeave'} in stored['flags']


def test_ot_preapproval_produces_a_detected_slip_and_removing_ot_deletes_it(demo, hr):
    execute("""INSERT INTO ot_request (employee_id, local_date, requested_min, approved_min, status, decided_at)
               VALUES (%s, '2026-09-24', 120, 120, 'approved', now())""", [demo['sita']])
    insert_punch(demo['sita'], '2026-09-24', '10:00')
    p2 = insert_punch(demo['sita'], '2026-09-24', '19:30')
    d = recompute(hr, demo['sita'], '2026-09-24')
    assert (d['day']['ot']['minutes'], d['day']['ot']['status']) == (90, 'Detected')
    slip = one("SELECT detected_min, status FROM ot_slip WHERE employee_id = %s AND local_date = '2026-09-24'", [demo['sita']])
    assert (slip['detected_min'], slip['status']) == (90, 'detected')
    # A superseding row hides the late punch: no OT any more, so the detected slip goes.
    execute("""INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date,
                 direction, verify_mode, source, idempotency_key, supersedes_id)
               SELECT device_id, device_sn, vendor, device_user_pin, employee_id, punched_at - interval '90 minutes', device_local_time,
                      local_date, direction, verify_mode, 'manual', idempotency_key || '-fix', id FROM punch_event WHERE id = %s""", [p2])
    assert recompute(hr, demo['sita'], '2026-09-24')['day']['ot']['minutes'] == 0
    assert one("SELECT 1 AS x FROM ot_slip WHERE employee_id = %s AND local_date = '2026-09-24'", [demo['sita']]) is None


def test_weekly_ot_cap_counts_sunday_to_saturday(demo):
    for d, ot in (('2026-09-13', 100), ('2026-09-19', 50), ('2026-09-20', 999), ('2026-09-12', 999)):
        execute("INSERT INTO attendance_day (employee_id, local_date, version, status, ot_min, inputs_hash) VALUES (%s, %s, 1, 'Present', %s, 'h')",
                [demo['ram'], d, ot])
    assert repo.ot_minutes_this_week(demo['ram'], '2026-09-16') == 150


def test_a_locked_day_is_never_rewritten(demo, hr):
    insert_punch(demo['ram'], '2026-09-14', '10:00', '2')
    recompute(hr, demo['ram'], '2026-09-14')
    execute("UPDATE attendance_day SET locked_at = now() WHERE employee_id = %s AND local_date = '2026-09-14'", [demo['ram']])
    insert_punch(demo['ram'], '2026-09-14', '18:00', '2')
    r = recompute(hr, demo['ram'], '2026-09-14')
    assert (r['locked'], r['changed']) == (True, False)
    assert is_attendance_locked(demo['ram'], '2026-09-14')


def test_a_locked_payroll_period_freezes_days_without_a_row(demo):
    execute("""INSERT INTO payroll_period (company_id, bs_year, bs_month, start_ad, end_ad, days, attendance_locked_at)
               VALUES (%s, 2083, 6, '2026-09-17', '2026-10-17', 31, now())""", [demo['company_id']])
    r = recompute_day(demo['sita'], '2026-09-21')
    assert (r.locked, r.version, r.changed) == (True, 0, False)
    assert is_attendance_locked(demo['sita'], '2026-09-21')


def test_validates_input_and_404s_unknown_employees(demo, hr):
    with pytest.raises(ValidationError):
        recompute(hr, 'nope', '2026-09-21')
    with pytest.raises(ValidationError):
        recompute(hr, demo['sita'], '2026-13-40')
    with pytest.raises(NotFoundError):
        recompute(hr, '00000000-0000-7000-8000-0000000000ff', '2026-09-21')


def test_employee_cannot_recompute(demo, login_as):
    me = login_as(['employee'], demo['sita']).user
    with pytest.raises(ForbiddenError):
        recompute(me, demo['sita'], '2026-09-21')
    # but can read their own days and punches
    assert api.days(me, {'employeeId': demo['sita'], 'from': '2026-09-01', 'to': '2026-09-02'}) == []
    with pytest.raises(ForbiddenError):
        api.punches(me, {'employeeId': demo['ram'], 'from': '2026-09-01', 'to': '2026-09-02'})


# ---------------------------------------------------------------- tenancy and manager scope

def _other_company_hr(demo):
    execute("INSERT INTO company (id, code, name_en) VALUES (%s, 'OTHER', 'Other Co')", [OTHER])
    auth.create_user(company_id=OTHER, email='hr@other.local', name='o', password=PW, roles=['hr'])
    return auth.resolve_session(auth.login('hr@other.local', PW).token)


def test_hr_of_another_company_cannot_read_or_recompute(demo):
    tok = _other_company_hr(demo)
    with pytest.raises(NotFoundError):
        api.days(tok, {'employeeId': demo['sita'], 'from': '2026-09-01', 'to': '2026-09-30'})
    with pytest.raises(NotFoundError):
        recompute(tok, demo['sita'], '2026-09-21')
    with pytest.raises(ForbiddenError, match='not in your company'):
        api.recompute_range(tok, {'employeeIds': [demo['sita']], 'from': '2026-09-01', 'to': '2026-09-02'})


def test_manager_sees_direct_reports_only(demo, login_as):
    mgr = login_as(['manager'], demo['ram']).user
    with pytest.raises(ForbiddenError):
        api.days(mgr, {'employeeId': demo['sita'], 'from': '2026-09-01', 'to': '2026-09-02'})
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s AND upper_inf(valid)', [demo['ram'], demo['sita']])
    assert api.days(mgr, {'employeeId': demo['sita'], 'from': '2026-09-01', 'to': '2026-09-02'}) == []
    insert_punch(demo['sita'], '2026-09-01', '10:00')
    assert len(api.punches(mgr, {'employeeId': demo['sita'], 'from': '2026-09-01', 'to': '2026-09-02'})) == 1


def test_exceptions_queue_matches_flags_by_containment_and_includes_auto_clock_outs(demo, hr):
    insert_punch(demo['sita'], '2026-09-29', '10:00')
    insert_punch(demo['sita'], '2026-09-29', '18:00', source='auto_clock_out')
    recompute(hr, demo['sita'], '2026-09-29')
    ex = api.exceptions(hr, {'from': '2026-09-29', 'to': '2026-09-29'})
    assert (demo['sita'], 'Present') in [(str(e['employee_id']), e['status']) for e in ex]
    assert {'kind': 'AutoClockOut'} in ex[0]['flags']


def test_exceptions_for_a_manager_cover_their_team_only(demo, login_as, hr):
    mgr = login_as(['manager'], demo['ram']).user
    insert_punch(demo['sita'], '2026-09-29', '10:00')
    recompute(hr, demo['sita'], '2026-09-29')
    assert api.exceptions(mgr, {'from': '2026-09-29', 'to': '2026-09-29'}) == []
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    assert [e['status'] for e in api.exceptions(mgr, {'from': '2026-09-29', 'to': '2026-09-29'})] == ['Incomplete']
    with pytest.raises(ForbiddenError):
        api.exceptions(login_as(['employee'], demo['sita']).user, {'from': '2026-09-29', 'to': '2026-09-29'})


def test_recompute_range_queues_pairs_above_the_inline_limit_and_rejects_empty_ranges(demo, hr):
    r = api.recompute_range(hr, {'employeeIds': [demo['sita'], demo['ram']], 'from': '2026-09-01', 'to': '2026-10-02'})
    assert r == {'queued': 64, 'recomputed': 0}
    assert one('SELECT count(*)::int AS n FROM recompute_queue')['n'] == 64
    from hajiri.core.errors import BadRequestError
    with pytest.raises(BadRequestError, match='range must be 1 to 62 days'):
        api.recompute_range(hr, {'employeeIds': [demo['sita']], 'from': '2026-09-03', 'to': '2026-09-01'})
    small = api.recompute_range(hr, {'employeeIds': [demo['sita']], 'from': '2026-09-01', 'to': '2026-09-03'})
    assert small == {'queued': 0, 'recomputed': 3}


def test_team_today(demo, login_as, hr):
    insert_punch(demo['sita'], '2026-09-21', '10:00')
    recompute(hr, demo['sita'], '2026-09-21')
    t = api.today(hr, {'date': '2026-09-21'})
    assert [(r['emp_code'], r['status']) for r in t['rows']] == [('E001', 'Incomplete'), ('E002', None)]
    mgr = login_as(['manager'], demo['ram']).user
    assert [r['emp_code'] for r in api.today(mgr, {'date': '2026-09-21'})['rows']] == ['E002']
    ex = login_as(['executive']).user
    assert len(api.today(ex, {'date': '2026-09-21'})['rows']) == 2


def test_half_day_regularisation_is_a_paid_half_day(demo, hr):
    insert_punch(demo['sita'], '2026-09-30', '10:00')
    insert_punch(demo['sita'], '2026-09-30', '14:00')
    execute("""INSERT INTO regularisation_request (employee_id, local_date, kind, reason, status, decided_at)
               VALUES (%s, '2026-09-30', 'half_day', 'doctor', 'approved', now())""", [demo['sita']])
    r = recompute(hr, demo['sita'], '2026-09-30')
    assert r['day']['status'] == 'HalfDayLeave'
    assert {'kind': 'HalfDayLeave', 'half': 'second_half'} in r['day']['flags']
    assert {'kind': 'UnpaidLeave'} not in r['day']['flags']


def test_business_dates_are_nepal_dates():
    # The TS pool set the session TimeZone to Asia/Kathmandu so SQL current_date meant a Nepal date. The Django
    # connection runs in UTC, so services pass Nepal dates explicitly; this is the date they pass.
    assert npt_today(datetime(2026, 9, 21, 18, 30, tzinfo=timezone.utc)).isoformat() == '2026-09-22'  # 00:15 NPT
    assert npt_today(datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)).isoformat() == '2026-09-21'   # 23:45 NPT


# ---------------------------------------------------------------- queue helpers used by other modules

def test_recompute_queued_for_processes_this_employees_queue(demo):
    insert_punch(demo['sita'], '2026-09-21', '10:00')
    insert_punch(demo['sita'], '2026-09-21', '18:00')
    enqueue_recompute([(demo['sita'], '2026-09-21'), (demo['sita'], '2026-09-22'), (demo['ram'], '2026-09-21')], 'leave')
    assert recompute_queued_for(demo['sita']) == 2
    assert [str(r['employee_id']) for r in rows('SELECT employee_id FROM recompute_queue')] == [demo['ram']]
    assert list_days(demo['sita'], '2026-09-21', '2026-09-21')[0]['worked_min'] == 480


def test_dates_for_punch_and_recompute_for_punch(demo):
    from engines.attendance import local_time_ms
    assert dates_for_punch(local_time_ms('2026-09-22', '01:30')) == ['2026-09-21', '2026-09-22']
    assert dates_for_punch(local_time_ms('2026-09-22', '13:00')) == ['2026-09-22']
    insert_punch(demo['sita'], '2026-09-21', '10:00')
    pid = insert_punch(demo['sita'], '2026-09-21', '18:00')
    r = recompute_for_punch(demo['sita'], local_time_ms('2026-09-21', '18:00'), pid)
    assert r.day.local_date == '2026-09-21' and pid in r.day.punch_ids


def test_lock_attendance_locks_the_latest_versions(demo, hr):
    insert_punch(demo['sita'], '2026-09-21', '10:00')
    recompute(hr, demo['sita'], '2026-09-21')
    assert lock_attendance(demo['company_id'], '2026-09-01', '2026-09-30') == 1
    assert lock_attendance(demo['company_id'], '2026-09-01', '2026-09-30') == 0


def test_resolve_shift_prefers_regularisation_then_roster_then_assignment(demo):
    assert repo.resolve_shift(demo['sita'], '2026-09-21').id == demo['shift_id']
    night = one("""INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES (%s, 'NIGHT', 'Night', '22:00', '06:00')
                   RETURNING id""", [demo['company_id']])['id']
    execute("INSERT INTO roster (employee_id, local_date, shift_id) VALUES (%s, '2026-09-21', NULL)", [demo['sita']])
    assert repo.resolve_shift(demo['sita'], '2026-09-21') is None
    execute("""INSERT INTO regularisation_request (employee_id, local_date, kind, reason, status, decided_at, shift_id)
               VALUES (%s, '2026-09-21', 'shift_change', 'swap', 'approved', now(), %s)""", [demo['sita'], night])
    s = repo.resolve_shift(demo['sita'], '2026-09-21')
    assert (s.id, s.start, s.end) == (str(night), '22:00', '06:00')


def test_sql_nepal_date_expression_matches_npt_today(db):
    # Raw SQL uses (now() AT TIME ZONE 'Asia/Kathmandu')::date where the TS code used current_date.
    from hajiri.core.db import one as _one
    assert _one("SELECT (now() AT TIME ZONE 'Asia/Kathmandu')::date AS d")['d'] == npt_today()
