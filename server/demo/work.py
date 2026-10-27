"""Time tracking, field work and the roster (port of apps/api/src/db/demo-suite/work.ts)."""
from __future__ import annotations

import re
from datetime import timedelta

from hajiri.core.clock import now
from hajiri.core.db import execute, one, rows
from hajiri.field import services as field
from hajiri.shifts import services as shifts
from hajiri.timesheets import services as time_svc
from hajiri.workflow import service as workflow

from .context import DemoContext, already_seeded
from .kit import Actor, add_days_iso, js_day, npt_instant, pick, week_start


def is_workday(iso: str) -> bool:
    return js_day(iso) != 6  # Saturday is the weekly off


def approve(ctx: DemoContext, who: Actor, instance_id: str | None, reason: str | None = None) -> None:
    """Approve a workflow instance as the given actor (the inbox's one-tap approve)."""
    if instance_id:
        ctx.kit.call(f'POST /workflow/{instance_id}/approve', workflow.decide, who.user, instance_id, 'approve', reason=reason)


def instance_of(object_type: str, object_id: str) -> str | None:
    r = one('SELECT id FROM workflow_instance WHERE object_type = %s AND object_id = %s', [object_type, object_id])
    return str(r['id']) if r else None


def seed_time(ctx: DemoContext) -> None:
    """Clients, projects and activities; two weeks of time for the engineering and growth teams; timesheets in every state."""
    kit, hr = ctx.kit, ctx.hr
    if already_seeded(ctx, 'time', 'SELECT 1 FROM timesheet WHERE company_id = %s LIMIT 1', 'SELECT 1 FROM time_client WHERE company_id = %s LIMIT 1'):
        return
    nabil = kit.call('POST /time/clients', time_svc.create_client, hr.user, {'name': 'Nabil Bank', 'notes': 'GPU cluster for fraud models.'})
    kathmandu = kit.call('POST /time/clients', time_svc.create_client, hr.user, {'name': 'Kathmandu University AI Lab', 'notes': 'Research compute partnership.'})

    def proj(b: dict) -> str:
        return str(kit.call('POST /time/projects', time_svc.create_project, hr.user, b))
    p1 = proj({'code': 'NBL-GPU', 'name': 'Nabil GPU cluster rollout', 'clientId': str(nabil['id']), 'color': '#12773a', 'billable': True, 'billRate': 4500,
               'budgetHours': 400, 'allMembers': True})
    p2 = proj({'code': 'KU-LAB', 'name': 'KU AI Lab onboarding', 'clientId': str(kathmandu['id']), 'color': '#1c7ed6', 'billable': True, 'billRate': 3000,
               'budgetHours': 160, 'allMembers': True})
    p3 = proj({'code': 'CONSOLE', 'name': 'Cloud console v2', 'color': '#7048e8', 'billable': False, 'budgetHours': 600, 'allMembers': True})
    p4 = proj({'code': 'INTERNAL', 'name': 'Internal and admin', 'color': '#868e96', 'billable': False, 'allMembers': True})

    def act(name: str, project_id: str | None = None) -> str:
        return str(kit.call('POST /time/activities', time_svc.create_activity, hr.user, {'name': name, **({'projectId': project_id} if project_id else {})})['id'])
    dev, review, meet, sup = act('Development'), act('Code review'), act('Meetings'), act('Support')
    deploy = act('Deployment', p1)

    trackers = [e for e in ctx.employees if e.manager_id and re.search(r'engineer|designer|sales|success|growth|reliab|network|technician|qa',
                                                                         e.designation or '', re.I)][:8]
    this_week = week_start(ctx.today)
    weeks = [add_days_iso(this_week, -14), add_days_iso(this_week, -7), this_week]
    for i, e in enumerate(trackers):
        me = ctx.as_(e)
        for ws in weeks:
            for d in range(7):
                day = add_days_iso(ws, d)
                if not is_workday(day) or day > ctx.today:
                    continue
                first = pick([p1, p3, p2], i + d)
                plan = [
                    ('09:30', '12:30', first, deploy if first == p1 and (i + d) % 3 == 2 else dev,
                     pick(['GPU node health checks', 'Billing API endpoints', 'Terraform for the new racks', 'Console usage charts', 'Load test for inference API'], i + d)),
                    ('13:30', '15:00', pick([p3, p1], i), pick([review, dev], d), pick(['Reviewed pull requests', 'Pairing on the scheduler', 'Fixed flaky tests'], i + d)),
                    ('15:15', '16:00', p4, meet, pick(['Team stand-up and planning', 'Weekly sync', 'Customer call notes'], d)),
                    ('16:00', '17:45', pick([p2, p1, p3], i + d + 1), pick([sup, dev], i),
                     pick(['Helped KU researchers with quota', 'Investigated latency alert', 'Documentation updates'], i * d)),
                ]
                for start, end, project_id, activity_id, note in plan:
                    if npt_instant(day, end) > now():
                        continue
                    kit.call('POST /time/entries', time_svc.add_entry, me.user,
                             {'date': day, 'start': start, 'end': end, 'projectId': project_id, 'activityId': activity_id, 'note': note})
        # Two weeks ago: submitted and approved. Last week: submitted (most approved, some waiting). This week: still open.
        t1 = str(kit.call('POST /time/timesheets', time_svc.submit_timesheet, me.user, {'weekStart': weeks[0]}))
        t2 = str(kit.call('POST /time/timesheets', time_svc.submit_timesheet, me.user,
                          {'weekStart': weeks[1], 'note': 'Worked late on Thursday for the Nabil cut-over.' if i % 3 == 0 else None}))
        mgr = ctx.manager_of(e)
        approve(ctx, mgr, instance_of('timesheet', t1))
        if i % 3 != 0:
            approve(ctx, mgr, instance_of('timesheet', t2), 'Looks right, thanks.')


PLACES = [
    ('Nabil Bank, Kamaladi', 'Quarterly business review', 27.7085, 85.3206, 'Agreed to extend the GPU contract by six months.'),
    ('Kathmandu University, Dhulikhel', 'Onboarding workshop for researchers', 27.6186, 85.5386, 'Ran the workshop for 24 students; follow-up on quotas next week.'),
    ('Leapfrog Technology, Charkhal', 'Product demo', 27.6986, 85.3192, 'Demoed inference endpoints; they want a trial.'),
    ('Datacenter, Bhaisepati', 'Rack installation check', 27.6466, 85.3036, 'Two new racks powered and tested.'),
    ('Himalayan Bank, Thamel', 'Contract negotiation', 27.7149, 85.3123, 'Sent the revised quote.'),
    ('Ncell office, Lainchaur', 'Partnership discussion', 27.7181, 85.3155, 'Next step: technical deep dive.'),
]


def seed_field(ctx: DemoContext) -> None:
    """Office location with a geofence, a reception kiosk, field staff with client visits, and breaks."""
    kit, hr = ctx.kit, ctx.hr
    if already_seeded(ctx, 'field', 'SELECT 1 FROM work_break WHERE company_id = %s LIMIT 1', 'SELECT 1 FROM kiosk WHERE company_id = %s LIMIT 1'):
        return
    locs = kit.call('GET /field/locations', field.locations, hr.user)['locations']
    hq = str(locs[0]['id']) if locs else None
    if not hq:
        hq = kit.call('POST /field/locations', field.create_location, hr.user,
                      {'name': 'Kathmandu HQ', 'address': 'Baluwatar, Kathmandu', 'branchId': ctx.employees[0].branch_id, 'lat': 27.7286, 'lng': 85.3311,
                       'radiusM': 150})['id']
    kit.call('POST /field/locations', field.create_location, hr.user,
             {'name': 'Datacenter, Bhaisepati', 'address': 'Bhaisepati, Lalitpur', 'lat': 27.6466, 'lng': 85.3036, 'radiusM': 200})
    kit.call('POST /field/kiosks', field.create_kiosk, hr.user,
             {'name': 'Reception tablet', 'branchId': ctx.employees[0].branch_id, 'locationId': hq, 'selfieRequired': True})

    field_staff = [e for e in ctx.employees if re.search(r'sales|success|growth|technician', e.designation or '', re.I)][:4]
    for e in field_staff:
        kit.call(f'PUT /field/staff/{e.id}', field.update_staff, hr.user, e.id, {'fieldWork': True})
    current = now()
    for i, e in enumerate(field_staff):
        for back in range(1, 7):
            day = add_days_iso(ctx.today, -back)
            if not is_workday(day) or (back + i) % 2:
                continue
            place, purpose, lat, lng, outcome = pick(PLACES, i + back)
            start = npt_instant(day, pick(['10:15', '11:00', '14:00', '15:30'], i + back))
            end = start + timedelta(minutes=75 + ((i * 17 + back * 13) % 90))
            execute("""INSERT INTO field_visit (company_id, employee_id, local_date, place, purpose, started_at, start_lat, start_lng, start_accuracy_m,
                                                ended_at, end_lat, end_lng, end_accuracy_m, outcome)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [ctx.company_id, e.id, day, place, purpose, start, lat + 0.0003, lng - 0.0002, 12 + i, end, lat, lng, 9 + back, outcome])
    # Breaks over the last few days (one ran over its limit), recorded as the app would.
    office = [e for e in ctx.employees if e not in field_staff][:8]
    for i, e in enumerate(office):
        for back in range(4):
            day = add_days_iso(ctx.today, -back)
            if not is_workday(day):
                continue
            lunch_start = npt_instant(day, pick(['13:00', '13:10', '13:20', '12:50'], i + back))
            lunch_min = 72 if back == 0 and i == 2 else 35 + ((i + back) % 4) * 5
            if back == 0 and npt_instant(day, '14:30') > current:
                continue
            execute("""INSERT INTO work_break (company_id, employee_id, local_date, kind, paid, max_min, started_at, ended_at, minutes, over_limit, source)
                       VALUES (%s, %s, %s, 'lunch', false, 60, %s, %s, %s, %s, 'web')""",
                    [ctx.company_id, e.id, day, lunch_start, lunch_start + timedelta(minutes=lunch_min), lunch_min, lunch_min > 60])
            if (i + back) % 2 == 0:
                tea = npt_instant(day, '16:00')
                if tea < current:
                    execute("""INSERT INTO work_break (company_id, employee_id, local_date, kind, paid, max_min, started_at, ended_at, minutes, over_limit, source)
                               VALUES (%s, %s, %s, 'tea', true, 15, %s, %s, 12, false, 'web')""",
                            [ctx.company_id, e.id, day, tea, tea + timedelta(minutes=12)])


def seed_roster(ctx: DemoContext) -> None:
    """A morning and an evening shift for operations, next week's roster published, and a shift swap waiting for approval."""
    kit, hr = ctx.kit, ctx.hr
    if rows("SELECT 1 FROM shift WHERE company_id = %s AND code = 'EARLY'", [ctx.company_id]):
        return
    early_id = kit.call('POST /shifts', shifts.create_shift, hr.user, {'code': 'EARLY', 'nameEn': 'Early 07:00-15:00', 'start': '07:00', 'end': '15:00', 'graceInMin': 10})['id']
    late_id = kit.call('POST /shifts', shifts.create_shift, hr.user, {'code': 'LATE', 'nameEn': 'Late 13:00-21:00', 'start': '13:00', 'end': '21:00', 'graceInMin': 10})['id']
    g = one("SELECT id FROM shift WHERE company_id = %s AND code = 'GEN'", [ctx.company_id])
    gen = str(g['id']) if g else None
    team = [e for e in ctx.employees if re.search(r'reliab|network|datacenter|technician|support|success|office', e.designation or '', re.I)][:6]
    if len(team) < 2:
        return
    ws = add_days_iso(week_start(ctx.today), 7)
    plan = []
    for i, e in enumerate(team):
        for d in range(7):
            plan.append({'employeeId': e.id, 'localDate': add_days_iso(ws, d),
                         'shiftId': None if d == 6 else late_id if (i + d) % 3 == 0 else early_id if (i + d) % 3 == 1 else gen})
    kit.call('PUT /shifts/roster', shifts.save_roster, hr.user, {'rows': plan})
    kit.call('POST /shifts/roster/publish', shifts.publish_roster, hr.user, {'from': ws, 'to': add_days_iso(ws, 6)})
    # Swap: two people on different shifts the same day.
    for d in range(6):
        day = add_days_iso(ws, d)
        a = next((r for r in plan if r['localDate'] == day and r['shiftId'] == early_id), None)
        b = next((r for r in plan if r['localDate'] == day and r['shiftId'] == late_id), None)
        if a and b:
            ea = next(t for t in team if t.id == a['employeeId'])
            eb = next(t for t in team if t.id == b['employeeId'])
            s = kit.call('POST /field/swaps', field.request_swap, ctx.as_(ea).user, {'colleagueId': eb.id, 'localDate': day, 'reason': 'Family function in the morning.'})
            kit.call(f"POST /field/swaps/{s['id']}/accept", field.accept_swap, ctx.as_(eb).user, s['id'], {'note': 'Happy to swap.'})
            break

