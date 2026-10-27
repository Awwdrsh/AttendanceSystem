"""Port of apps/api/test/worker.test.ts and the worker resilience case of qa-foundation.test.ts."""
from datetime import datetime, timedelta, timezone


from engines.attendance import local_time_ms

from hajiri.attendance.repo import from_ms
from hajiri.attendance.service import enqueue_recompute
from hajiri.core.clock import now as _now
from hajiri.core.db import execute, one, rows
from hajiri.worker import registry
from hajiri.worker.runner import Scheduler
from hajiri.worker.tasks import (
    MAX_RECOMPUTE_ATTEMPTS, auto_clock_out, detect_offline_devices, drain_recompute_queue, ensure_punch_partitions, run_once,
    sla_reminders,
)
from tests.core.punches import insert_punch


def punch(emp, d, hhmm, pin):
    return insert_punch(emp, d, hhmm, pin, key_prefix='w')


def count(sql, params=()):
    return one(sql, params)['n']


def test_run_once_drains_the_queue_and_computes_days(demo):
    punch(demo['sita'], '2026-09-21', '10:00', '1')
    punch(demo['sita'], '2026-09-21', '18:00', '1')
    enqueue_recompute([{'employeeId': demo['sita'], 'localDate': '2026-09-21'}, {'employeeId': demo['ram'], 'localDate': '2026-09-21'}], 'test')
    out = run_once(['queue'])
    assert out['queue'] == {'processed': 2, 'failed': 0, 'dropped': 0}
    assert count('SELECT count(*)::int AS n FROM recompute_queue') == 0
    day = one("SELECT status, worked_min FROM v_attendance_latest WHERE employee_id = %s AND local_date = '2026-09-21'", [demo['sita']])
    assert (day['status'], day['worked_min']) == ('Present', 480)


def test_nightly_sweep_queues_the_last_three_days_for_every_active_employee(demo):
    out = run_once(['nightly'])
    assert out['nightly'] == 2 * 3
    assert count('SELECT count(*)::int AS n FROM recompute_queue') == 6
    assert 'filesPurged' in out  # the daily module jobs run after the sweep
    run_once(['queue'])
    assert count('SELECT count(*)::int AS n FROM recompute_queue') == 0


def test_device_offline_notifies_once_escalates_recovers_and_dedupes(demo, login_as):
    hr = login_as(['hr'])
    now = _now()
    execute('UPDATE device SET last_seen_at = %s WHERE id = %s', [now - timedelta(minutes=5), demo['device_id']])
    assert detect_offline_devices(now) == {'offline': 1, 'escalated': 0, 'recovered': 0}
    assert detect_offline_devices(now + timedelta(minutes=1)) == {'offline': 0, 'escalated': 0, 'recovered': 0}
    n1 = count("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'device.offline' AND channel = 'in_app' AND user_id = %s", [hr.user_id])
    assert n1 == 1
    # Notification rows are stamped with the database clock; move them to "now" so the offsets below are exact.
    execute("UPDATE notification_outbox SET created_at = %s WHERE template = 'device.offline'", [now])
    assert detect_offline_devices(now + timedelta(minutes=12)) == {'offline': 0, 'escalated': 1, 'recovered': 0}
    execute("UPDATE notification_outbox SET created_at = %s WHERE template = 'device.offline' AND payload->>'stage' = '15min'", [now + timedelta(minutes=12)])
    assert detect_offline_devices(now + timedelta(minutes=13)) == {'offline': 0, 'escalated': 0, 'recovered': 0}
    execute('UPDATE device SET last_seen_at = %s WHERE id = %s', [now + timedelta(minutes=14), demo['device_id']])
    # The 'online' notice is stamped now(); with the device back the outage closes once.
    assert detect_offline_devices(now + timedelta(minutes=14, seconds=10)) == {'offline': 0, 'escalated': 0, 'recovered': 1}
    execute("UPDATE notification_outbox SET created_at = %s WHERE template = 'device.online'", [now + timedelta(minutes=14, seconds=10)])
    assert detect_offline_devices(now + timedelta(minutes=14, seconds=20)) == {'offline': 0, 'escalated': 0, 'recovered': 0}


def test_sla_reminders_go_to_the_approver_after_24h_and_hr_after_48h_once_per_day(demo, login_as):
    hr = login_as(['hr'])
    created = _now() - timedelta(hours=50)
    execute("""INSERT INTO workflow_instance (company_id, object_type, object_id, employee_id, steps, created_at)
               VALUES (%s, 'leave_request', uuidv7(), %s, '[{"role":"manager"}]', %s)""", [demo['company_id'], demo['sita'], created])
    assert sla_reminders() == {'reminded': 1, 'escalated': 1}
    assert sla_reminders() == {'reminded': 0, 'escalated': 0}
    n = count("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'workflow.escalation' AND channel = 'in_app' AND user_id = %s", [hr.user_id])
    assert n == 1


def test_sla_reminders_use_the_workflow_approvers_hook(demo, login_as, monkeypatch):
    login_as(['hr'])
    approver = login_as(['manager'])
    monkeypatch.setitem(registry._HOOKS, 'workflow.approvers_for_step', lambda company, emp, step: [approver.user_id])
    execute("""INSERT INTO workflow_instance (company_id, object_type, object_id, employee_id, steps, created_at)
               VALUES (%s, 'leave_request', uuidv7(), %s, '[{"role":"manager"}]', %s)""", [demo['company_id'], demo['sita'], _now() - timedelta(hours=30)])
    assert sla_reminders() == {'reminded': 1, 'escalated': 0}
    r = rows("SELECT user_id, title FROM notification_outbox WHERE template = 'workflow.sla' AND channel = 'in_app'")
    assert [str(x['user_id']) for x in r] == [approver.user_id]
    assert r[0]['title'] == 'Approval waiting 30 h: leave request from Sita Sharma'


def test_auto_clock_out_inserts_an_out_at_shift_end_after_the_delay(demo):
    execute("""UPDATE company_setting SET value = value || '{"autoClockOutAfterMin": 60}' WHERE company_id = %s AND key = 'attendance.policy'""",
            [demo['company_id']])
    punch(demo['ram'], '2026-09-22', '10:05', '2')
    enqueue_recompute([(demo['ram'], '2026-09-22')], 't')
    run_once(['queue'])
    assert one("SELECT status FROM v_attendance_latest WHERE employee_id = %s AND local_date = '2026-09-22'", [demo['ram']])['status'] == 'Incomplete'
    at = lambda hhmm: from_ms(local_time_ms('2026-09-22', hhmm))  # noqa: E731
    assert auto_clock_out(at('18:30')) == 0
    assert auto_clock_out(at('19:30')) == 1
    assert auto_clock_out(at('20:30')) == 0
    run_once(['queue'])
    day = one("SELECT status, worked_min FROM v_attendance_latest WHERE employee_id = %s AND local_date = '2026-09-22'", [demo['ram']])
    assert (day['status'], day['worked_min']) == ('Present', 475)
    note = rows("SELECT 1 FROM notification_outbox WHERE template = 'attendance.auto_clock_out' AND employee_id = %s AND channel = 'in_app'", [demo['ram']])
    assert len(note) == 1
    dev = one("SELECT serial_no, vendor FROM device WHERE serial_no = 'MANUAL-DEMO'")
    assert dev['vendor'] == 'manual'


def test_accrual_runs_once_per_company_for_the_previous_bs_month_and_is_recorded(demo, monkeypatch):
    calls = []
    monkeypatch.setitem(registry._HOOKS, 'leave.monthly_accrual', lambda c, y, m: (calls.append((c, y, m)), {'credited': 2})[1])
    monkeypatch.setitem(registry._HOOKS, 'leave.catch_up', lambda c, today: None)
    at = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
    assert run_once(['accrual'], at)['accrual'] == 1
    assert run_once(['accrual'], at)['accrual'] == 0
    s = one("SELECT value FROM company_setting WHERE key = 'leave.accrual.last'")
    assert s['value']['month'] == '2083-05' and s['value']['credited'] == 2
    assert calls == [(demo['company_id'], 2083, 5)]


def test_leave_catch_up_runs_every_time_and_even_without_an_accrual_hook(demo, monkeypatch):
    """runLeaveCatchUp ran for every company on every accrual pass, before the once-a-month check."""
    seen = []
    monkeypatch.delitem(registry._HOOKS, 'leave.monthly_accrual', raising=False)
    monkeypatch.setitem(registry._HOOKS, 'leave.catch_up', lambda c, today: seen.append((c, today)))
    at = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
    assert run_once(['accrual'], at)['accrual'] == 0
    assert seen == [(demo['company_id'], '2026-09-21')]
    assert one("SELECT 1 AS x FROM company_setting WHERE key = 'leave.accrual.last'") is None
    # With the accrual hook, the catch-up still runs on the second pass when the month is already done.
    monkeypatch.setitem(registry._HOOKS, 'leave.monthly_accrual', lambda c, y, m: {})
    run_once(['accrual'], at)
    run_once(['accrual'], at)
    assert len(seen) == 3


def test_a_row_that_keeps_failing_is_retried_with_backoff_then_dropped_with_an_hr_notice(demo, login_as):
    hr = login_as(['hr'])
    ghost = '00000000-0000-7000-8000-00000000dead'
    execute('ALTER TABLE recompute_queue DROP CONSTRAINT recompute_queue_employee_id_fkey')
    enqueue_recompute([(ghost, '2026-09-01')], 'test')
    dropped = 0
    for _ in range(MAX_RECOMPUTE_ATTEMPTS + 1):
        execute("UPDATE recompute_queue SET enqueued_at = now() - interval '1 hour'")
        dropped += drain_recompute_queue()['dropped']
    assert dropped == 1
    assert count('SELECT count(*)::int AS n FROM recompute_queue') == 0
    # The employee does not exist, so there is no company to tell (as in the TS worker).
    assert one("SELECT 1 AS x FROM notification_outbox WHERE template = 'attendance.recompute_failed' AND user_id = %s", [hr.user_id]) is None


def test_dropped_recompute_of_a_real_employee_notifies_hr(demo, login_as, monkeypatch):
    hr = login_as(['hr'])
    import hajiri.worker.tasks as tasks

    def broken(emp, day):
        raise RuntimeError('shift is broken')
    monkeypatch.setattr(tasks, 'recompute_day', broken)
    enqueue_recompute([(demo['sita'], '2026-09-01')], 'test')
    for _ in range(MAX_RECOMPUTE_ATTEMPTS):
        execute("UPDATE recompute_queue SET enqueued_at = now() - interval '1 hour'")
        drain_recompute_queue()
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'attendance.recompute_failed' AND user_id = %s", [hr.user_id])
    assert n['title'] == 'Attendance for 2026-09-01 could not be computed'
    assert 'failed 5 times (shift is broken)' in n['body']


def test_failed_row_is_requeued_with_attempts_and_error(demo):
    ghost = '00000000-0000-7000-8000-00000000beef'
    execute('ALTER TABLE recompute_queue DROP CONSTRAINT recompute_queue_employee_id_fkey')
    enqueue_recompute([(ghost, '2026-09-01')], 'test')
    assert drain_recompute_queue() == {'processed': 0, 'failed': 1, 'dropped': 0}
    r = one('SELECT attempts, reason, last_error, enqueued_at > now() AS later FROM recompute_queue')
    assert (r['attempts'], r['reason'], r['later']) == (1, 'retry', True)
    assert 'not found' in r['last_error']


def test_punch_partitions_are_created_ahead(db):
    made = ensure_punch_partitions(datetime(2031, 2, 10, tzinfo=timezone.utc), months_ahead=1)
    assert made == ['punch_event_2031_02', 'punch_event_2031_03']
    assert ensure_punch_partitions(datetime(2031, 2, 10, tzinfo=timezone.utc), months_ahead=1) == []
    b = one("""SELECT pg_get_expr(c.relpartbound, c.oid) AS b FROM pg_class c WHERE c.relname = 'punch_event_2031_02'""")
    # Shown in the session time zone (UTC): Nepal midnight on the 1st is 18:15 UTC the day before.
    assert b['b'] == "FOR VALUES FROM ('2031-01-31 18:15:00+00') TO ('2031-02-28 18:15:00+00')"


def test_registered_daily_jobs_run_and_a_failure_does_not_stop_the_others(demo, monkeypatch):
    ran = []
    monkeypatch.setitem(registry._DAILY, 'ok', registry.Job('ok', lambda now: ran.append('ok') or 3))

    def boom(now):
        raise RuntimeError('broken job')
    monkeypatch.setitem(registry._DAILY, 'boom', registry.Job('boom', boom))
    out = run_once(['nightly'])
    assert out['ok'] == 3 and out['boom'] == {'error': 'broken job'} and ran == ['ok']


def test_scheduler_cadence(db):
    s = Scheduler(senders=[])
    t0 = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
    first = s.due_jobs(t0)
    assert first[:6] == ['queue', 'deliver', 'offline', 'sla', 'autoClockOut', 'digest'] and 'accrual' in first
    assert s.due_jobs(t0 + timedelta(seconds=15)) == ['queue']
    assert s.due_jobs(t0 + timedelta(seconds=30)) == ['queue', 'deliver']
    midnight = datetime(2026, 9, 21, 18, 50, tzinfo=timezone.utc)  # 00:35 NPT on the 22nd
    jobs = s.due_jobs(midnight)
    assert {'nightly', 'accrual', 'partitions'} <= set(jobs)
    assert 'nightly' not in s.due_jobs(midnight + timedelta(minutes=1))


def test_scheduler_tick_runs_under_the_worker_lock(demo):
    s = Scheduler(senders=[])
    out = s.tick(datetime(2026, 9, 21, 18, 50, tzinfo=timezone.utc))
    assert out is not None and 'nightly' in out
    # The nightly marker stops a second scheduler (another process) from running it again that day.
    assert 'nightly' not in Scheduler(senders=[]).due_jobs(datetime(2026, 9, 21, 18, 55, tzinfo=timezone.utc))
