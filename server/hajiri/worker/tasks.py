"""
Background work (port of apps/api/src/worker.ts) on Django's task framework (django.tasks, database backend
django_tasks_db).

Each job is a plain function of `now`, safe to run again, in its own transaction; `run_job` is the @task the scheduler
(runner.py) enqueues and `manage.py db_worker` (or `run_worker`) executes. A job never runs twice at the same time:
`run_job` holds a PostgreSQL advisory lock named after the job, so several task workers can share the queue. The
attendance work queue itself (recompute_queue) and the notification outbox stay tables of their own, drained by the
'queue' and 'deliver' jobs.

    from hajiri.worker.tasks import run_job
    run_job.enqueue('digest')                 # run soon, in a task worker
    run_once(['queue', 'deliver'])            # run now, in this process (tests, run_worker --once)
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from django.db import IntegrityError, connection, transaction
from django.db.models import Exists, IntegerField, OuterRef, Subquery, TextField
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Coalesce, Now
from django.tasks import task

from engines.attendance import DAY_MIN, MIN_MS, local_date_of, local_time_ms, parse_hhmm
from engines.bs_calendar import BsRangeError, ad_to_bs

from hajiri.attendance.repo import from_ms, latest_days, to_ms  # latest_days re-exported for callers
from hajiri.attendance.service import date_range, enqueue_recompute, recompute_day
from hajiri.core.clock import NPT, now as _now
from hajiri.core.crypto import sha256_hex
from hajiri.core.settings_store import put_setting, setting_subquery
from hajiri.db.models import (
    AttendanceDay, Company, Device, Employee, NotificationOutbox, PunchEvent, PunchPhoto, RecomputeQueue, WorkflowInstance,
)
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


def _take_batch(batch: int) -> list[dict]:
    """Claim and remove up to `batch` due rows; rows locked by another worker are skipped (FOR UPDATE SKIP LOCKED)."""
    with transaction.atomic():
        taken = list(RecomputeQueue.objects.select_for_update(skip_locked=True).filter(enqueued_at__lte=Now())
                     .order_by('enqueued_at').values('employee_id', 'local_date', 'attempts')[:batch])
        if taken:
            RecomputeQueue.objects.filter(pk__in=[(t['employee_id'], t['local_date']) for t in taken]).delete()
    return taken


def _drain_batch(batch: int) -> dict:
    taken = _take_batch(batch)
    processed = failed = dropped = 0
    for row in taken:
        emp, day = str(row['employee_id']), row['local_date'].isoformat()
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
                        company_id = Employee.objects.filter(pk=emp).values_list('company_id', flat=True).first()
                        if company_id:
                            notify_role(str(company_id), 'hr', template='attendance.recompute_failed',
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
                        RecomputeQueue.objects.bulk_create(
                            [RecomputeQueue(employee_id=emp, local_date=day, reason='retry', attempts=attempts,
                                            last_error=message, enqueued_at=_now() + timedelta(minutes=attempts))],
                            update_conflicts=True, unique_fields=['employee', 'local_date'],
                            update_fields=['attempts', 'last_error', 'enqueued_at'])
                except Exception:  # noqa: BLE001
                    log.exception('could not re-queue recompute')
    return {'processed': processed, 'failed': failed, 'dropped': dropped, 'taken': len(taken)}


# ---------------------------------------------------------------- (2) nightly

@transaction.atomic
def purge_expired_photos(now: datetime | None = None) -> int:
    """Wipe work-from-home photos older than each company's retention period (default 90 days); the row stays."""
    now = now or _now()
    companies = Company.objects.annotate(
        days=Coalesce(setting_subquery('attendance.policy', 'wfhPhotoRetentionDays', company_ref=OuterRef('pk'),
                                       output_field=IntegerField()), 90))
    purged = 0
    for c in companies:
        purged += (PunchPhoto.objects.for_company(c.pk)
                   .filter(image_enc__isnull=False, created_at__lt=now - timedelta(days=c.days))
                   .update(image_enc=None, purged_at=now))
    return purged


@transaction.atomic
def nightly_sweep(now: datetime | None = None) -> int:
    """Queue the last 3 days for every active employee."""
    now = now or _now()
    today = local_date_of(to_ms(now))
    start = local_date_of(local_time_ms(today, '00:00') - 2 * DAY_MIN * MIN_MS)
    dates = date_range(start, today)
    emps = [str(e) for e in Employee.objects.filter(status=Employee.Status.ACTIVE).values_list('id', flat=True)]
    enqueue_recompute([(e, d) for e in emps for d in dates], 'nightly')
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
    companies = Company.objects.annotate(last=setting_subquery('leave.accrual.last', 'month', company_ref=OuterRef('pk')))
    ran = 0
    for c in companies:
        cid = str(c.pk)
        if catch_up:
            try:
                with transaction.atomic():
                    catch_up(cid, today)
            except Exception:  # noqa: BLE001
                log.exception('leave catch-up failed for company %s', cid)
        if accrue is None or (c.last and c.last >= key):
            continue
        try:
            with transaction.atomic():
                summary = accrue(cid, prev_y, prev_m) or {}
                put_setting(cid, 'leave.accrual.last',
                            json.loads(json.dumps({'month': key, 'ranAt': _js_iso(now), **summary}, default=str)))
            ran += 1
        except Exception:  # noqa: BLE001
            log.exception('leave accrual failed for company %s month %s', cid, key)
    return ran


def _js_iso(d: datetime) -> str:
    d = d.astimezone(timezone.utc)
    return d.strftime('%Y-%m-%dT%H:%M:%S.') + f'{d.microsecond // 1000:03d}Z'


# ---------------------------------------------------------------- (3) terminals offline

def _outbox_for(template: str, key: str = 'deviceId', ref=None):
    """Notification outbox rows of a template whose payload names the outer row (payload->>'deviceId' = id::text)."""
    return NotificationOutbox.objects.annotate(_ref=KT(f'payload__{key}')).filter(
        template=template, _ref=Cast(ref if ref is not None else OuterRef('id'), TextField()))


@transaction.atomic
def detect_offline_devices(now: datetime | None = None) -> dict:
    """Offline after 120 s of silence (once per outage), escalated at 15 min, and a notice when it is back."""
    now = now or _now()
    offline_q = _outbox_for('device.offline').order_by('-created_at')
    devices = (Device.objects.filter(status=Device.Status.ACTIVE, company__isnull=False, last_seen_at__isnull=False)
               .exclude(vendor__in=('mobile', 'kiosk', 'manual'))
               .annotate(last_offline=Subquery(offline_q.values('created_at')[:1]),
                         last_offline_stage=Subquery(offline_q.annotate(_stage=KT('payload__stage')).values('_stage')[:1]),
                         last_online=Subquery(_outbox_for('device.online').order_by('-created_at').values('created_at')[:1])))
    offline = escalated = recovered = 0
    for d in devices:
        silent = now - d.last_seen_at
        label = d.name or d.serial_no
        company, device_id = str(d.company_id), str(d.id)
        outage_open = d.last_offline is not None and (d.last_online is None or d.last_online < d.last_offline)
        if silent > OFFLINE_AFTER:
            minutes = int(silent.total_seconds() // 60)
            # A new outage: no open offline notice, or the last one predates the device's most recent contact.
            if not outage_open or d.last_offline < d.last_seen_at:
                notify_role(company, 'hr', template='device.offline', title=f'Terminal {label} is offline',
                            body=f'{label} has not contacted the server for {minutes} minutes. Punches are being saved on the device.',
                            payload={'deviceId': device_id, 'stage': '2min', 'minutes': minutes}, channels=('push',))
                offline += 1
            elif silent > OFFLINE_ESCALATE and d.last_offline_stage != '15min':
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
    day_ago = now - timedelta(hours=24)
    found = (WorkflowInstance.objects.filter(state=WorkflowInstance.State.PENDING, created_at__lt=day_ago)
             .select_related('employee')
             .annotate(sla_hours=Coalesce(setting_subquery('notifications.policy', 'slaHours', output_field=IntegerField()), 24),
                       escalation_hours=Coalesce(setting_subquery('notifications.policy', 'escalationHours',
                                                                  output_field=IntegerField()), 48),
                       reminded_today=Exists(_outbox_for('workflow.sla', 'instanceId').filter(created_at__gt=day_ago)),
                       escalated_today=Exists(_outbox_for('workflow.escalation', 'instanceId').filter(created_at__gt=day_ago))))
    approvers = registry.hook('workflow.approvers_for_step')
    reminded = escalated = 0
    for w in found:
        company = str(w.company_id)
        age_h = (now - w.created_at).total_seconds() / 3600
        steps = w.steps or []
        step = steps[w.current_step] if 0 <= w.current_step < len(steps) else None
        name = w.employee.name_en if w.employee_id else None
        subject = w.object_type.replace('_', ' ') + (f' from {name}' if name else '')
        payload = {'instanceId': str(w.id), 'objectType': w.object_type}
        if age_h >= w.sla_hours and not w.reminded_today:
            # The same resolution as the inbox (line manager, heads, groups, named people, delegates); HR as the last resort.
            targets = list(approvers(company, w.employee_id and str(w.employee_id), step)) if (step and approvers) else []
            if not targets:
                targets = user_ids_with_role(company, 'hr')
            for uid in targets:
                notify(company_id=company, user_id=str(uid), template='workflow.sla',
                       title=f'Approval waiting {int(age_h)} h: {subject}',
                       body=f'A {subject} has been waiting for your decision for {int(age_h)} hours.', payload=payload,
                       channels=('push', 'email'))
            if targets:
                reminded += 1
        if age_h >= w.escalation_hours and not w.escalated_today:
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
    since = date.fromisoformat(local_date_of(now_ms)) - timedelta(days=3)
    companies = Company.objects.annotate(after=Coalesce(setting_subquery(
        'attendance.policy', 'autoClockOutAfterMin', company_ref=OuterRef('pk'), output_field=IntegerField()), 0))
    done = 0
    for c in companies:
        if c.after <= 0:
            continue
        cid = str(c.pk)
        with transaction.atomic():
            auto_punch = PunchEvent.objects.filter(employee=OuterRef('employee'), local_date=OuterRef('local_date'),
                                                   source='auto_clock_out')
            days = list(latest_days().for_company(cid)
                        .filter(status=AttendanceDay.Status.INCOMPLETE, locked_at__isnull=True, local_date__gte=since,
                                shift__isnull=False)
                        .exclude(Exists(auto_punch))
                        .values('employee_id', 'local_date', 'employee__emp_code', 'shift__start_time', 'shift__end_time'))
            if not days:
                continue
            device = ensure_pseudo_device(cid, 'MANUAL')
            for d in days:
                local_date = d['local_date'].isoformat()
                start_hm, end_hm = d['shift__start_time'].strftime('%H:%M'), d['shift__end_time'].strftime('%H:%M')
                start, end = parse_hhmm(start_hm), parse_hhmm(end_hm)
                end_ms = local_time_ms(local_date, end_hm, 1 if end <= start else 0)
                if now_ms < end_ms + c.after * MIN_MS:
                    continue
                emp = str(d['employee_id'])
                try:
                    with transaction.atomic():
                        PunchEvent.objects.create(
                            device_id=device['id'], device_sn=device['serialNo'], vendor=device['vendor'],
                            device_user_pin=d['employee__emp_code'], employee_id=emp, punched_at=from_ms(end_ms),
                            device_local_time=npt_local_time(end_ms), local_date=local_date,
                            direction=PunchEvent.Direction.OUT, verify_mode='auto', source='auto_clock_out',
                            raw={'auto': True, 'afterMin': c.after, 'regularisationRequired': True},
                            idempotency_key=sha256_hex(f'auto|{emp}|{local_date}'))
                except IntegrityError:
                    continue            # already clocked out automatically (same idempotency key)
                enqueue_recompute([(emp, local_date)], 'auto_clock_out')
                notify(company_id=cid, user_id=user_id_for_employee(emp), employee_id=emp, template='attendance.auto_clock_out',
                       title=f'Missing OUT punch on {local_date}',
                       body=(f'No OUT punch was recorded on {local_date}, so the shift end time was used. If you left at a '
                             'different time, submit a correction.'), payload={'localDate': local_date}, channels=('push',))
                done += 1
    return done


# ---------------------------------------------------------------- (7) manager digest

def digest_if_due(now: datetime | None = None) -> int:
    """Once per company per day at the configured digest time (default 10:45 Nepal time)."""
    now = now or _now()
    today = local_date_of(to_ms(now))
    t = now.astimezone(NPT)
    npt_min = t.hour * 60 + t.minute
    sent_today = (NotificationOutbox.objects.annotate(_date=KT('payload__date'))
                  .filter(company=OuterRef('pk'), template='attendance.digest', _date=today))
    companies = (Company.objects
                 .annotate(digest=setting_subquery('notifications.policy', 'digestTime', company_ref=OuterRef('pk')),
                           sent=Exists(sent_today)))
    ran = 0
    for c in companies:
        if c.sent:
            continue
        h, _, m = (c.digest or '10:45').partition(':')
        if npt_min < int(h or 10) * 60 + int(m or 45):
            continue
        with transaction.atomic():
            daily_digest(str(c.pk), today)
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
        with connection.cursor() as cur:
            # Raw SQL: Django's introspection leaves partitions out, so ask the catalog directly.
            cur.execute('SELECT to_regclass(%s) IS NOT NULL', [f'public.{name}'])
            exists = cur.fetchone()[0]
        if not exists:
            with transaction.atomic(), connection.cursor() as cur:
                # Raw SQL: DDL for a partition of the partitioned punch_event table has no ORM equivalent.
                cur.execute(f"CREATE TABLE IF NOT EXISTS public.{name} PARTITION OF public.punch_event "
                            f"FOR VALUES FROM ('{d.isoformat()} 00:00:00+05:45') TO ('{nxt.isoformat()} 00:00:00+05:45')")
            made.append(name)
        d = nxt
    return made


# ---------------------------------------------------------------- upkeep of the task queue itself

def prune_task_results(now: datetime | None = None, keep: timedelta = timedelta(days=2)) -> int:
    """Finished task results older than `keep` (the scheduler enqueues a job every 15 s, so they add up)."""
    from django_tasks_db.models import DBTaskResult
    now = now or _now()
    deleted, _ = DBTaskResult.objects.finished().filter(finished_at__lt=now - keep).delete()
    return deleted


# ---------------------------------------------------------------- run once / as a task

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
    """Run the given jobs once, now, in this process, regardless of schedule (tests, `run_worker --once`, run_job)."""
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
            elif job == 'pruneTasks':
                out['pruneTasks'] = prune_task_results(now)
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


@contextmanager
def job_lock(name: str):
    """A PostgreSQL session advisory lock per job, so two task workers never run the same job at once. Yields
    whether this process got it."""
    with connection.cursor() as cur:
        # Raw SQL: advisory locks are PostgreSQL functions with no ORM API.
        cur.execute('SELECT pg_try_advisory_lock(hashtext(%s))', [f'hajiri.job:{name}'])
        got = cur.fetchone()[0]
    try:
        yield got
    finally:
        if got:
            with connection.cursor() as cur:
                # Raw SQL: releases the advisory lock taken above.
                cur.execute('SELECT pg_advisory_unlock(hashtext(%s))', [f'hajiri.job:{name}'])


@task
def run_job(name: str, at: str | None = None) -> dict:
    """The background task for one job (a name from JOB_NAMES, 'pruneTasks', or a registered periodic job). `at` is
    the scheduler's tick time (ISO 8601), so a job that waited in the queue still works on the intended moment.
    Returns the job's JSON summary (stored as the task result)."""
    now = datetime.fromisoformat(at) if at else _now()
    with job_lock(name) as got:
        if not got:
            return {name: {'skipped': 'already running in another worker'}}
        return json.loads(json.dumps(run_once([name], now), default=str))
