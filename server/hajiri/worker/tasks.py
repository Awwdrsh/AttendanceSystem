"""
Background work (port of apps/api/src/worker.ts). Every job is a plain function of `now`, safe to run again, and
runs in its own transaction; the scheduler in runner.py decides when. The work queue itself (recompute_queue,
notification_outbox) lives in PostgreSQL, so a single-machine install needs nothing but the database.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta

from django.db import transaction

from engines.attendance import DAY_MIN, MIN_MS, local_date_of, local_time_ms, parse_hhmm
from engines.bs_calendar import BsRangeError, ad_to_bs

from hajiri.attendance.repo import from_ms, to_ms
from hajiri.attendance.service import date_range, enqueue_recompute, recompute_day
from hajiri.core.clock import NPT, now as _now
from hajiri.core.crypto import sha256_hex
from hajiri.core.db import execute, one, rows
from hajiri.notifications.service import (
    daily_digest, deliver_queued, notify, notify_role, senders_from_env, user_id_for_employee, user_ids_with_role,
)

from . import registry

log = logging.getLogger('hajiri.worker')

MAX_RECOMPUTE_ATTEMPTS = 5
OFFLINE_AFTER = timedelta(seconds=120)
OFFLINE_ESCALATE = timedelta(minutes=15)

JOB_NAMES = ('queue', 'deliver', 'offline', 'sla', 'autoClockOut', 'nightly', 'accrual', 'digest', 'partitions')


# ---------------------------------------------------------------- (1) recompute queue

def drain_recompute_queue(*, batch: int = 200, budget_seconds: float = 10.0) -> dict:
    """Drain recompute_queue in batches until it is empty or the time budget is spent. A failing row is re-queued with
    attempts + 1 (sorting after fresh work) and dropped after MAX_RECOMPUTE_ATTEMPTS with an HR notification, so one
    poisoned row never blocks the queue."""
    deadline = time.monotonic() + budget_seconds
    total = {'processed': 0, 'failed': 0, 'dropped': 0}
    while True:
        r = _drain_batch(batch)
        for k in total:
            total[k] += r[k]
        if r['taken'] < batch or time.monotonic() >= deadline:
            return total


def _drain_batch(batch: int) -> dict:
    with transaction.atomic():
        taken = rows("""
            DELETE FROM recompute_queue WHERE (employee_id, local_date) IN (
              SELECT employee_id, local_date FROM recompute_queue WHERE enqueued_at <= now() ORDER BY enqueued_at
               LIMIT %s FOR UPDATE SKIP LOCKED)
            RETURNING employee_id, local_date::text AS local_date, attempts""", [batch])
    processed = failed = dropped = 0
    for row in taken:
        emp, day = str(row['employee_id']), row['local_date']
        try:
            recompute_day(emp, day)
            processed += 1
        except Exception as e:  # noqa: BLE001 - any failure is retried, then reported
            attempts = row['attempts'] + 1
            message = str(e)[:500]
            if attempts >= MAX_RECOMPUTE_ATTEMPTS:
                dropped += 1
                log.error('recompute failed repeatedly; dropped employee=%s date=%s attempts=%s', emp, day, attempts, exc_info=True)
                try:
                    with transaction.atomic():
                        c = one('SELECT company_id FROM employee WHERE id = %s', [emp])
                        if c:
                            notify_role(str(c['company_id']), 'hr', template='attendance.recompute_failed',
                                        title=f'Attendance for {day} could not be computed',
                                        body=(f"Recomputing one employee's attendance for {day} failed {attempts} times ({message}). "
                                              'Check the shift and punches, then recompute from the exceptions queue.'),
                                        payload={'employeeId': emp, 'localDate': day})
                except Exception:  # noqa: BLE001
                    log.exception('could not notify HR about a dropped recompute')
            else:
                failed += 1
                log.warning('recompute failed; re-queued employee=%s date=%s attempts=%s: %s', emp, day, attempts, message)
                # Backoff: the row sorts after fresh work for `attempts` minutes.
                try:
                    with transaction.atomic():
                        execute("""INSERT INTO recompute_queue (employee_id, local_date, reason, attempts, last_error, enqueued_at)
                                   VALUES (%s, %s, 'retry', %s::int, %s, %s::timestamptz)
                                   ON CONFLICT (employee_id, local_date) DO UPDATE SET attempts = EXCLUDED.attempts,
                                     last_error = EXCLUDED.last_error, enqueued_at = EXCLUDED.enqueued_at""",
                                [emp, day, attempts, message, _now() + timedelta(minutes=attempts)])
                except Exception:  # noqa: BLE001
                    log.exception('could not re-queue recompute')
    return {'processed': processed, 'failed': failed, 'dropped': dropped, 'taken': len(taken)}


# ---------------------------------------------------------------- (2) nightly

@transaction.atomic
def purge_expired_photos(now: datetime | None = None) -> int:
    """Wipe work-from-home photos older than each company's retention period (default 90 days); the row stays."""
    now = now or _now()
    return execute("""
        UPDATE punch_photo ph SET image_enc = NULL, purged_at = %s
          FROM company c LEFT JOIN company_setting s ON s.company_id = c.id AND s.key = 'attendance.policy'
         WHERE ph.company_id = c.id AND ph.image_enc IS NOT NULL
           AND ph.created_at < %s::timestamptz - make_interval(days => coalesce((s.value->>'wfhPhotoRetentionDays')::int, 90))""",
                   [now, now])


@transaction.atomic
def nightly_sweep(now: datetime | None = None) -> int:
    """Queue the last 3 days for every active employee."""
    now = now or _now()
    today = local_date_of(to_ms(now))
    start = local_date_of(local_time_ms(today, '00:00') - 2 * DAY_MIN * MIN_MS)
    dates = date_range(start, today)
    emps = rows("SELECT id FROM employee WHERE status = 'active'")
    enqueue_recompute([(str(e['id']), d) for e in emps for d in dates], 'nightly')
    return len(emps) * len(dates)


def monthly_accrual_if_due(now: datetime | None = None) -> int:
    """Leave accrual for the previous BS month, once per company (guarded by company_setting leave.accrual.last), and
    the idempotent catch-up for missed months. The leave module registers both as hooks."""
    now = now or _now()
    today = local_date_of(to_ms(now))
    try:
        bs = ad_to_bs(today)
    except BsRangeError:
        return 0
    prev_y, prev_m = (bs.year - 1, 12) if bs.month == 1 else (bs.year, bs.month - 1)
    key = f'{prev_y}-{prev_m:02d}'
    catch_up = registry.hook('leave.catch_up')
    accrue = registry.hook('leave.monthly_accrual')
    if accrue is None:
        log.warning('no leave.monthly_accrual hook registered; leave accrual skipped')
    companies = rows("""SELECT c.id, (SELECT value->>'month' FROM company_setting s WHERE s.company_id = c.id
                                        AND s.key = 'leave.accrual.last') AS last FROM company c""")
    ran = 0
    for c in companies:
        cid = str(c['id'])
        if catch_up:
            try:
                with transaction.atomic():
                    catch_up(cid, today)
            except Exception:  # noqa: BLE001
                log.exception('leave catch-up failed for company %s', cid)
        if accrue is None or (c['last'] and c['last'] >= key):
            continue
        try:
            with transaction.atomic():
                summary = accrue(cid, prev_y, prev_m) or {}
                execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'leave.accrual.last', %s)
                           ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                        [cid, json.dumps({'month': key, 'ranAt': _js_iso(now), **summary}, default=str)])
            ran += 1
        except Exception:  # noqa: BLE001
            log.exception('leave accrual failed for company %s month %s', cid, key)
    return ran


def _js_iso(d: datetime) -> str:
    from datetime import timezone
    d = d.astimezone(timezone.utc)
    return d.strftime('%Y-%m-%dT%H:%M:%S.') + f'{d.microsecond // 1000:03d}Z'


# ---------------------------------------------------------------- (3) terminals offline

@transaction.atomic
def detect_offline_devices(now: datetime | None = None) -> dict:
    """Offline after 120 s of silence (once per outage), escalated at 15 min, and a notice when it is back."""
    now = now or _now()
    devices = rows("""
        SELECT d.id, d.company_id, d.serial_no, d.name, d.last_seen_at,
               (SELECT max(created_at) FROM notification_outbox n WHERE n.template = 'device.offline' AND n.payload->>'deviceId' = d.id::text) AS last_offline,
               (SELECT payload->>'stage' FROM notification_outbox n WHERE n.template = 'device.offline' AND n.payload->>'deviceId' = d.id::text
                 ORDER BY created_at DESC LIMIT 1) AS last_offline_stage,
               (SELECT max(created_at) FROM notification_outbox n WHERE n.template = 'device.online' AND n.payload->>'deviceId' = d.id::text) AS last_online
          FROM device d WHERE d.status = 'active' AND d.company_id IS NOT NULL AND d.vendor NOT IN ('mobile', 'kiosk', 'manual')
           AND d.last_seen_at IS NOT NULL""")
    offline = escalated = recovered = 0
    for d in devices:
        silent = now - d['last_seen_at']
        label = d['name'] or d['serial_no']
        company, device_id = str(d['company_id']), str(d['id'])
        outage_open = d['last_offline'] is not None and (d['last_online'] is None or d['last_online'] < d['last_offline'])
        if silent > OFFLINE_AFTER:
            minutes = int(silent.total_seconds() // 60)
            # A new outage: no open offline notice, or the last one predates the device's most recent contact.
            if not outage_open or d['last_offline'] < d['last_seen_at']:
                notify_role(company, 'hr', template='device.offline', title=f'Terminal {label} is offline',
                            body=f'{label} has not contacted the server for {minutes} minutes. Punches are being saved on the device.',
                            payload={'deviceId': device_id, 'stage': '2min', 'minutes': minutes}, channels=('push',))
                offline += 1
            elif silent > OFFLINE_ESCALATE and d['last_offline_stage'] != '15min':
                notify_role(company, 'hr', template='device.offline', title=f'Terminal {label} offline for {minutes} minutes',
                            body=f'{label} is still offline. Check power and network at the terminal.',
                            payload={'deviceId': device_id, 'stage': '15min', 'minutes': minutes}, channels=('push', 'sms'))
                escalated += 1
        elif outage_open:
            notify_role(company, 'hr', template='device.online', title=f'Terminal {label} is back online',
                        body=f'{label} reconnected and is sending its buffered punches.', payload={'deviceId': device_id})
            recovered += 1
    return {'offline': offline, 'escalated': escalated, 'recovered': recovered}


# ---------------------------------------------------------------- (5) approval SLA

@transaction.atomic
def sla_reminders(now: datetime | None = None) -> dict:
    """Approval reminders after the SLA (24 h) to the approver, escalation to HR (48 h), once per instance per day."""
    now = now or _now()
    today = now.astimezone(NPT).date().isoformat()
    found = rows("""
        SELECT w.id, w.company_id, w.object_type, w.employee_id, w.steps, w.current_step, w.created_at, e.name_en,
               coalesce((p.value->>'slaHours')::int, 24) AS sla_hours, coalesce((p.value->>'escalationHours')::int, 48) AS escalation_hours,
               EXISTS (SELECT 1 FROM notification_outbox n WHERE n.template = 'workflow.sla' AND n.payload->>'instanceId' = w.id::text
                         AND n.created_at > %(now)s::timestamptz - interval '24 hours') AS reminded_today,
               EXISTS (SELECT 1 FROM notification_outbox n WHERE n.template = 'workflow.escalation' AND n.payload->>'instanceId' = w.id::text
                         AND n.created_at > %(now)s::timestamptz - interval '24 hours') AS escalated_today
          FROM workflow_instance w LEFT JOIN employee e ON e.id = w.employee_id
          LEFT JOIN company_setting p ON p.company_id = w.company_id AND p.key = 'notifications.policy'
         WHERE w.state = 'pending' AND w.created_at < %(now)s::timestamptz - interval '24 hours'""", {'now': now, 'today': today})
    approvers = registry.hook('workflow.approvers_for_step')
    reminded = escalated = 0
    for w in found:
        company = str(w['company_id'])
        age_h = (now - w['created_at']).total_seconds() / 3600
        steps = w['steps'] or []
        step = steps[w['current_step']] if 0 <= w['current_step'] < len(steps) else None
        subject = w['object_type'].replace('_', ' ') + (f" from {w['name_en']}" if w['name_en'] else '')
        payload = {'instanceId': str(w['id']), 'objectType': w['object_type']}
        if age_h >= w['sla_hours'] and not w['reminded_today']:
            # The same resolution as the inbox (line manager, heads, groups, named people, delegates); HR as the last resort.
            targets = list(approvers(company, w['employee_id'] and str(w['employee_id']), step)) if (step and approvers) else []
            if not targets:
                targets = user_ids_with_role(company, 'hr')
            for uid in targets:
                notify(company_id=company, user_id=str(uid), template='workflow.sla',
                       title=f'Approval waiting {int(age_h)} h: {subject}',
                       body=f'A {subject} has been waiting for your decision for {int(age_h)} hours.', payload=payload,
                       channels=('push', 'email'))
            if targets:
                reminded += 1
        if age_h >= w['escalation_hours'] and not w['escalated_today']:
            notify_role(company, 'hr', template='workflow.escalation', title=f'Escalation: {subject} unanswered for {int(age_h)} h',
                        body=(f'No decision has been made on a {subject} for {int(age_h)} hours. Please follow up or decide '
                              'on behalf of the approver.'), payload=payload, channels=('email',))
            escalated += 1
    return {'reminded': reminded, 'escalated': escalated}


# ---------------------------------------------------------------- (6) auto clock-out

# Pseudo devices for non-terminal punches: the same helpers the punch module uses (punch/devices.ts).
from hajiri.punch.services import ensure_pseudo_device, npt_local_time  # noqa: E402


def auto_clock_out(now: datetime | None = None) -> int:
    """For Incomplete days of the last 3 days, an OUT at shift end once the company's delay has passed, then a
    recompute and a notice asking for a correction if the person left at another time."""
    now = now or _now()
    now_ms = to_ms(now)
    companies = rows("""SELECT c.id, coalesce((s.value->>'autoClockOutAfterMin')::int, 0) AS after FROM company c
                         LEFT JOIN company_setting s ON s.company_id = c.id AND s.key = 'attendance.policy'""")
    done = 0
    for c in companies:
        if c['after'] <= 0:
            continue
        cid = str(c['id'])
        with transaction.atomic():
            days = rows("""
                SELECT v.employee_id, v.local_date::text AS local_date, e.emp_code, s.start_time::text, s.end_time::text
                  FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id JOIN shift s ON s.id = v.shift_id
                 WHERE e.company_id = %s AND v.status = 'Incomplete' AND v.locked_at IS NULL AND v.local_date >= (%s::date - 3)
                   AND NOT EXISTS (SELECT 1 FROM punch_event p WHERE p.employee_id = v.employee_id AND p.local_date = v.local_date
                                     AND p.source = 'auto_clock_out')""", [cid, local_date_of(now_ms)])
            if not days:
                continue
            device = ensure_pseudo_device(cid, 'MANUAL')
            for d in days:
                start, end = parse_hhmm(d['start_time'][:5]), parse_hhmm(d['end_time'][:5])
                end_ms = local_time_ms(d['local_date'], d['end_time'][:5], 1 if end <= start else 0)
                if now_ms < end_ms + c['after'] * MIN_MS:
                    continue
                emp = str(d['employee_id'])
                ins = one("""
                    INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time,
                      local_date, direction, verify_mode, source, raw, idempotency_key)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'out', 'auto', 'auto_clock_out', %s, %s)
                    ON CONFLICT (idempotency_key, punched_at) DO NOTHING RETURNING id""",
                          [device['id'], device['serialNo'], device['vendor'], d['emp_code'], emp, from_ms(end_ms),
                           npt_local_time(end_ms), d['local_date'],
                           json.dumps({'auto': True, 'afterMin': c['after'], 'regularisationRequired': True}),
                           sha256_hex(f"auto|{emp}|{d['local_date']}")])
                if not ins:
                    continue
                enqueue_recompute([(emp, d['local_date'])], 'auto_clock_out')
                notify(company_id=cid, user_id=user_id_for_employee(emp), employee_id=emp, template='attendance.auto_clock_out',
                       title=f"Missing OUT punch on {d['local_date']}",
                       body=(f"No OUT punch was recorded on {d['local_date']}, so the shift end time was used. If you left at a "
                             'different time, submit a correction.'), payload={'localDate': d['local_date']}, channels=('push',))
                done += 1
    return done


# ---------------------------------------------------------------- (7) manager digest

def digest_if_due(now: datetime | None = None) -> int:
    """Once per company per day at the configured digest time (default 10:45 Nepal time)."""
    now = now or _now()
    today = local_date_of(to_ms(now))
    t = now.astimezone(NPT)
    npt_min = t.hour * 60 + t.minute
    companies = rows("""
        SELECT c.id, (SELECT value->>'digestTime' FROM company_setting s WHERE s.company_id = c.id AND s.key = 'notifications.policy') AS digest,
               (SELECT max(created_at)::text FROM notification_outbox n WHERE n.company_id = c.id AND n.template = 'attendance.digest'
                  AND n.payload->>'date' = %s) AS last FROM company c""", [today])
    ran = 0
    for c in companies:
        if c['last']:
            continue
        h, _, m = (c['digest'] or '10:45').partition(':')
        if npt_min < int(h or 10) * 60 + int(m or 45):
            continue
        with transaction.atomic():
            daily_digest(str(c['id']), today)
        ran += 1
    return ran


# ---------------------------------------------------------------- (8) punch partitions

def ensure_punch_partitions(now: datetime | None = None, months_ahead: int = 3) -> list[str]:
    """Monthly punch_event partitions (Nepal-time month boundaries, like the ones the schema created) for this month and
    the next `months_ahead`; returns the partitions created."""
    now = now or _now()
    d = now.astimezone(NPT).date().replace(day=1)
    made = []
    for _ in range(months_ahead + 1):
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        name = f'punch_event_{d:%Y_%m}'
        exists = one('SELECT to_regclass(%s) IS NOT NULL AS x', [f'public.{name}'])['x']
        if not exists:
            with transaction.atomic():
                execute(f"CREATE TABLE IF NOT EXISTS public.{name} PARTITION OF public.punch_event "
                        f"FOR VALUES FROM ('{d.isoformat()} 00:00:00+05:45') TO ('{nxt.isoformat()} 00:00:00+05:45')")
            made.append(name)
        d = nxt
    return made


# ---------------------------------------------------------------- run once

def run_daily_module_jobs(now: datetime) -> dict:
    out: dict = {}
    for job in registry.daily_jobs():
        try:
            with transaction.atomic():
                out[job.name] = job.fn(now)
        except Exception as e:  # noqa: BLE001 - a failing job never stops the others
            log.exception('module job %s failed', job.name)
            out[job.name] = {'error': str(e)}
    return out


def run_once(jobs=JOB_NAMES, now: datetime | None = None, senders: list | None = None) -> dict:
    """Run the given jobs once, now, regardless of schedule (tests, the run_worker --once command, admin tooling)."""
    now = now or _now()
    out: dict = {}
    for job in jobs:
        try:
            if job == 'queue':
                out['queue'] = drain_recompute_queue()
            elif job == 'deliver':
                # Not one transaction: a row marked sent must stay marked even if a later row's update fails.
                out['deliver'] = deliver_queued(senders if senders is not None else senders_from_env(), now)
            elif job == 'offline':
                out['offline'] = detect_offline_devices(now)
            elif job == 'sla':
                out['sla'] = sla_reminders(now)
            elif job == 'autoClockOut':
                out['autoClockOut'] = auto_clock_out(now)
            elif job == 'nightly':
                out['nightly'] = nightly_sweep(now)
                out['photosPurged'] = purge_expired_photos(now)
                out.update(run_daily_module_jobs(now))
            elif job == 'accrual':
                out['accrual'] = monthly_accrual_if_due(now)
            elif job == 'digest':
                out['digest'] = digest_if_due(now)
            elif job == 'partitions':
                out['partitions'] = ensure_punch_partitions(now)
            else:
                periodic = {j.name: j for j in registry.periodic_jobs()}
                if job not in periodic:
                    raise ValueError(f'unknown job {job}')
                with transaction.atomic():
                    out[job] = periodic[job].fn(now)
        except Exception as e:  # noqa: BLE001
            log.exception('worker job %s failed', job)
            out[job] = {'error': str(e)}
    return out
