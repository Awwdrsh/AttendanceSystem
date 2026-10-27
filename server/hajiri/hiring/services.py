"""
Hiring and applicant tracking (port of apps/api/src/modules/hiring/service.ts and routes.ts): requisitions approved
through the workflow engine, job openings with screening questions and a transparent score, the candidate pipeline,
interviews and scorecards, offers approved by a second HR user, hire-into-employee, reports, the retention policy
and the nightly purge of candidates who were not selected.

Bodies use the TS camelCase shape and are validated with hajiri.core.schema, so API-style error messages match.
Views build those bodies from the forms. Every function takes the signed-in AuthUser and scopes by its company.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from django.db import transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_module, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import NPT
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import (MISSING, Arr, Bool, Enum, Hhmm, IsoDate, Money, Num, Obj, Record, S, Str, Uuid, _fail, _Issue,
                                _Issues, parse, parse_uuid)
from hajiri.files.store import attach_file, delete_file, read_file
from hajiri.integrations.hooks import HireEvent, on_candidate_hired, run_hire_hooks  # noqa: F401  (re-exported)
from hajiri.notifications.service import notify, user_id_for_employee, user_ids_with_role
from hajiri.workflow.service import (DecisionContext, cancel_workflow, npt_today, register_handler,
                                     register_summariser, start_workflow)

from .offer_letter import render_offer_letter

# ---------------------------------------------------------------- types and validation

DEFAULT_STAGES = ['Applied', 'Screening', 'Interview', 'Offer', 'Hired']
SOURCES = ('careers', 'referral', 'manual', 'job_board', 'other')
WORK_MODES = ('onsite', 'remote', 'hybrid')
EMPLOYMENT_TYPES = ('full_time', 'part_time', 'contract', 'internship', 'temporary')
RECOMMENDATIONS = ('strong_no', 'no', 'yes', 'strong_yes')
OPEN_OFFER = ('pending_approval', 'approved', 'accepted')

RULE = Obj({'equals': Enum(('yes', 'no'), optional=True), 'min': Num(min=0, max=1_000_000, optional=True)}, strict=True)

QUESTION = Obj({
    'id': Str(regex=r'^q[0-9a-z]{1,12}$', regex_message='question id like q1'),
    'kind': Enum(('yes_no', 'number', 'text')),
    'label': Str(trim=True, min=3, max=200),
    'required': Bool(default=True),
    'knockout': RULE.__class__(RULE.fields, strict=True, optional=True),
    'preferred': RULE.__class__(RULE.fields, strict=True, optional=True),
})


class _Stages(Arr):
    """z.array(...).min(2).max(10) with the three refinements of stagesSchema (each failing one is an issue)."""

    def __init__(self, **kw):
        super().__init__(Str(trim=True, min=2, max=40), min=2, max=10, **kw)

    def check(self, v, path):
        s = super().check(v, path)
        issues = []
        if len({x.lower() for x in s}) != len(s):
            issues.append(_Issue(path, 'stage names must be different'))
        if s[0] != 'Applied':
            issues.append(_Issue(path, 'the first stage must be "Applied"'))
        if s[-1] != 'Hired':
            issues.append(_Issue(path, 'the last stage must be "Hired"'))
        if issues:
            raise _Issues(issues)
        return s


def _opt_text(mx: int) -> Str:
    return Str(trim=True, max=mx, nullish=True)


JOB_FIELDS: dict[str, S] = {
    'requisitionId': Uuid(nullish=True),
    'title': Str(trim=True, min=2, max=120),
    'description': Str(max=10_000, default=''),
    'requirements': Str(max=10_000, default=''),
    'category': _opt_text(60),
    'location': _opt_text(120),
    'workMode': Enum(WORK_MODES, default='onsite'),
    'employmentType': Enum(EMPLOYMENT_TYPES, default='full_time'),
    'experienceMin': Num(min=0, max=60, nullish=True),
    'experienceMax': Num(min=0, max=60, nullish=True),
    'salaryMin': Money(nullish=True),
    'salaryMax': Money(nullish=True),
    'showSalary': Bool(default=False),
    'openings': Num(int_=True, min=1, max=500, default=1),
    'deadline': IsoDate(nullish=True),
    'orgUnitId': Uuid(nullish=True),
    'branchId': Uuid(nullish=True),
    'hiringManagerId': Uuid(nullish=True),
    'stages': _Stages(optional=True),
    'questions': Arr(QUESTION, max=15, optional=True),
}
JOB_BODY = Obj(JOB_FIELDS)


def _partial(fields: dict[str, S]) -> Obj:
    """jobBody.partial(): every field optional; zod still applies a field's default when the key is missing, so the
    caller keeps only the keys that were sent."""
    out = {}
    for k, s in fields.items():
        c = object.__new__(s.__class__)
        c.__dict__.update(s.__dict__)
        c.optional = True
        out[k] = c
    return Obj(out)


JOB_PATCH = _partial(JOB_FIELDS)


def js_num(v) -> str:
    """String(Number(v)) as JavaScript prints it: 3 not 3.0, 2.5 as 2.5."""
    f = float(v)
    return str(int(f)) if f.is_integer() else repr(f)


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def _js_number(a) -> float:
    """Number(a) in JavaScript."""
    if a is None or a is MISSING:
        return math.nan
    if isinstance(a, bool):
        return 1.0 if a else 0.0
    if isinstance(a, (int, float, Decimal)):
        return float(a)
    s = str(a).strip()
    if s == '':
        return 0.0
    try:
        return float(s)
    except ValueError:
        return math.nan


def _norm_num(n: float):
    return int(n) if float(n).is_integer() else n


def _as_yes_no(a) -> str | None:
    if a is True or a == 'yes':
        return 'yes'
    if a is False or a == 'no':
        return 'no'
    return None


def _meets(rule: dict, a) -> bool:
    if rule.get('equals'):
        return _as_yes_no(a) == rule['equals']
    if rule.get('min') is not None:
        n = a if isinstance(a, (int, float)) and not isinstance(a, bool) else _js_number(a)
        return math.isfinite(n) and n >= rule['min']
    return a is not None and a is not MISSING and str(a).strip() != ''


def score_application(job: dict, experience_years, answers: dict) -> dict:
    """
    Transparent, rule-based screening score (0-100): experience against the job's range is worth 40 points and each
    question with a preferred answer 20 points. Knock-out questions don't score; failing one flags the application.
    Returns {'score', 'knockedOut', 'detail': [{label, points, max, note}]}.
    """
    detail: list[dict] = []
    knocked_out = False
    mn = None if job.get('experience_min') is None else float(job['experience_min'])
    mx = None if job.get('experience_max') is None else float(job['experience_max'])
    if mn is not None or mx is not None:
        y = None if experience_years is None else float(experience_years)
        points = 0
        if y is None:
            note = 'Experience not given'
        elif mn is not None and y < mn:
            points = _js_round(40 * y / mn) if mn > 0 else 40
            note = f'{js_num(y)} years, below the {js_num(mn)} years asked for'
        elif mx is not None and y > mx:
            points = 30
            note = f'{js_num(y)} years, more than the {js_num(mx)} years in the range'
        else:
            points = 40
            note = f'{js_num(y)} years, within the range'
        detail.append({'label': 'Experience', 'points': points, 'max': 40, 'note': note})
    for q in job.get('questions') or []:
        a = answers.get(q['id'], MISSING)
        if q.get('knockout'):
            ok = _meets(q['knockout'], a)
            if not ok:
                knocked_out = True
            detail.append({'label': q['label'], 'points': 0, 'max': 0,
                           'note': 'Meets the must-have' if ok else 'Does not meet the must-have'})
        if q.get('preferred'):
            ok = _meets(q['preferred'], a)
            detail.append({'label': q['label'], 'points': 20 if ok else 0, 'max': 20,
                           'note': 'Preferred answer' if ok else 'Not the preferred answer'})
    total = sum(d['max'] for d in detail)
    earned = sum(d['points'] for d in detail)
    return {'score': _js_round(100 * earned / total) if total > 0 else None, 'knockedOut': knocked_out, 'detail': detail}


def _js_string(v) -> str:
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, float):
        return js_num(v)
    return str(v)


def clean_answers(questions: list[dict], raw: dict | None) -> dict:
    """Check answers against the job's questions: required ones present, types right, unknown ids dropped."""
    out: dict = {}
    src = raw or {}
    for q in questions:
        v = src.get(q['id'])
        empty = v is None or (isinstance(v, str) and v.strip() == '')
        if empty:
            if q.get('required', True):
                raise BadRequestError('Please answer every required question.')
            continue
        if q['kind'] == 'yes_no':
            yn = _as_yes_no(v)
            if not yn:
                raise BadRequestError('Answer the yes or no questions with yes or no.')
            out[q['id']] = yn
        elif q['kind'] == 'number':
            n = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else _js_number(v)
            if not math.isfinite(n) or n < 0 or n > 1_000_000:
                raise BadRequestError('Number answers must be a number.')
            out[q['id']] = _norm_num(n)
        else:
            out[q['id']] = _js_string(v).strip()[:1000]
    return out


# ---------------------------------------------------------------- timeline, e-mail, notifications

def add_event(*, company_id: str, application_id: str, actor_id: str | None, kind: str, body: str | None = None,
              data: dict | None = None) -> None:
    execute('INSERT INTO hiring_event (company_id, application_id, actor_id, kind, body, data) VALUES (%s, %s, %s, %s, %s, %s)',
            [company_id, application_id, actor_id, kind, body, json.dumps(data or {}, default=str)])


def email_candidate(*, company_id: str, application_id: str, to: str | None, template: str, title: str, body: str,
                    actor_id: str | None) -> bool:
    """Queue an e-mail to a candidate through the outbox (they are not users, so the address rides on the row) and log it."""
    if not to:
        return False
    execute("""INSERT INTO notification_outbox (company_id, user_id, employee_id, channel, template, title, body, payload, status, to_email)
               VALUES (%s, NULL, NULL, 'email', %s, %s, %s, %s, 'queued', %s)""",
            [company_id, template, title, body, json.dumps({'applicationId': str(application_id)}), to])
    add_event(company_id=company_id, application_id=application_id, actor_id=actor_id, kind='email', body=title,
              data={'template': template})
    return True


def company_name(company_id: str) -> str:
    r = one('SELECT name_en FROM company WHERE id = %s', [company_id])
    return r['name_en'] if r else 'our company'


def job_watchers(company_id: str, hiring_manager_id: str | None) -> list[str]:
    """HR plus the job's hiring manager: who hears about new applications."""
    ids = list(dict.fromkeys(user_ids_with_role(company_id, 'hr')))
    if hiring_manager_id:
        u = user_id_for_employee(str(hiring_manager_id))
        if u and u not in ids:
            ids.append(u)
    return ids


# ---------------------------------------------------------------- policy

def hiring_policy(company_id: str) -> dict:
    r = one("SELECT value FROM company_setting WHERE company_id = %s AND key = 'hiring.policy'", [company_id])
    v = (r and r['value']) or {}
    if not isinstance(v, dict):
        v = {}
    rd = v.get('retentionDays')
    return {'retentionDays': rd if isinstance(rd, (int, float)) and not isinstance(rd, bool) else 180,
            'careersEnabled': v.get('careersEnabled') is not False}


POLICY_BODY = Obj({'retentionDays': Num(int_=True, min=30, max=1095), 'careersEnabled': Bool()})


def set_policy(user: AuthUser, body: dict) -> dict:
    _need_module(user)
    company_id = company_id_of(user)
    b = parse(POLICY_BODY, body)
    with transaction.atomic():
        execute("""INSERT INTO company_setting (company_id, key, value, updated_by) VALUES (%s, 'hiring.policy', %s, %s)
                   ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()""",
                [company_id, json.dumps(b), user.id])
        audit('update', 'company_setting', actor_id=user.id, row_id=None, after={'key': 'hiring.policy', **b})
    return b


# ---------------------------------------------------------------- workflow handlers

def _decide_requisition(object_id: str, state: str, ctx: DecisionContext) -> None:
    q = one('SELECT company_id, raised_by, title, status FROM hiring_requisition WHERE id = %s FOR UPDATE', [object_id])
    if not q or q['status'] != 'pending':
        return
    if state == 'approved' and str(ctx.actor_id) == str(q['raised_by']):
        raise ForbiddenError('you raised this hiring request; someone else must approve it')
    execute('UPDATE hiring_requisition SET status = %s, decided_at = now(), decision_reason = %s WHERE id = %s',
            [state, ctx.reason, object_id])
    notify(company_id=str(q['company_id']), user_id=str(q['raised_by']), template=f'job_requisition.{state}',
           title='Hiring request approved' if state == 'approved' else 'Hiring request rejected',
           body=f'Your request to hire "{q["title"]}" was {state}{f": {ctx.reason}" if ctx.reason else ""}.',
           payload={'requisitionId': str(object_id)})


def _decide_offer(object_id: str, state: str, ctx: DecisionContext) -> None:
    o = one("""SELECT o.company_id, o.created_by, o.status, o.application_id, c.name FROM hiring_offer o
                 JOIN hiring_application a ON a.id = o.application_id JOIN hiring_candidate c ON c.id = a.candidate_id
                WHERE o.id = %s FOR UPDATE OF o""", [object_id])
    if not o or o['status'] != 'pending_approval':
        return
    if state == 'approved' and o['created_by'] is not None and str(ctx.actor_id) == str(o['created_by']):
        raise ForbiddenError('you drafted this offer; someone else must approve it')
    execute('UPDATE hiring_offer SET status = %s, decided_at = now() WHERE id = %s', [state, object_id])
    reason = f': {ctx.reason}' if ctx.reason else ''
    add_event(company_id=str(o['company_id']), application_id=str(o['application_id']), actor_id=ctx.actor_id, kind='offer',
              body=f'Offer {state}{reason}')
    if o['created_by']:
        notify(company_id=str(o['company_id']), user_id=str(o['created_by']), template=f'job_offer.{state}',
               title='Offer approved' if state == 'approved' else 'Offer rejected',
               body=f'The offer for {o["name"]} was {state}{reason}.',
               payload={'offerId': str(object_id), 'applicationId': str(o['application_id'])})


def _summarise_requisition(inst: dict) -> dict:
    return one("""SELECT q.title, q.designation, q.headcount, q.kind, q.salary_min, q.salary_max, q.justification, ou.name_en AS department
                    FROM hiring_requisition q LEFT JOIN org_unit ou ON ou.id = q.org_unit_id WHERE q.id = %s""",
               [inst['object_id']]) or {}


def _summarise_offer(inst: dict) -> dict:
    return one("""SELECT o.designation, o.salary, o.join_date, c.name AS candidate, j.title AS job
                    FROM hiring_offer o JOIN hiring_application a ON a.id = o.application_id JOIN hiring_candidate c ON c.id = a.candidate_id
                    JOIN hiring_job j ON j.id = a.job_id WHERE o.id = %s""", [inst['object_id']]) or {}


def register_workflow() -> None:
    register_handler('job_requisition', _decide_requisition)
    register_handler('job_offer', _decide_offer)
    register_summariser('job_requisition', _summarise_requisition)
    register_summariser('job_offer', _summarise_offer)


register_workflow()


# ---------------------------------------------------------------- retention

def purge_rejected_candidates(now: datetime | None = None) -> int:
    """
    Daily: once a candidate has had no open application for the retention period (rejected or withdrawn), their name,
    contact details, CVs, cover notes, answers and notes are removed. Counts and stage history stay for reports.
    """
    now = now or datetime.now(timezone.utc)
    due = rows("""SELECT c.id, c.company_id FROM hiring_candidate c
                    LEFT JOIN company_setting s ON s.company_id = c.company_id AND s.key = 'hiring.policy'
                   WHERE c.purged_at IS NULL
                     AND EXISTS (SELECT 1 FROM hiring_application a WHERE a.candidate_id = c.id)
                     AND NOT EXISTS (SELECT 1 FROM hiring_application a WHERE a.candidate_id = c.id
                                      AND (a.status NOT IN ('rejected', 'withdrawn')
                                           OR coalesce(a.closed_at, a.created_at) > %s::timestamptz - make_interval(days => coalesce((s.value->>'retentionDays')::int, 180))))""",
               [now])
    n = 0
    for c in due:
        cid, co = str(c['id']), str(c['company_id'])
        with transaction.atomic():
            files = rows('SELECT id, cv_file_id FROM hiring_application WHERE candidate_id = %s', [cid])
            for f in files:
                if f['cv_file_id']:
                    delete_file(co, str(f['cv_file_id']))
            execute("UPDATE hiring_candidate SET name = 'Removed candidate', email = NULL, phone = NULL, purged_at = %s WHERE id = %s",
                    [now, cid])
            execute("""UPDATE hiring_application SET cv_file_id = NULL, cover_note = NULL, answers = '{}', current_employer = NULL,
                              tags = '{}', score_detail = '[]' WHERE candidate_id = %s""", [cid])
            app_ids = [str(f['id']) for f in files]
            execute("""UPDATE hiring_event SET body = NULL WHERE application_id = ANY(%s::uuid[])
                         AND kind IN ('note', 'email', 'rejected', 'applied')""", [app_ids])
            execute("""UPDATE hiring_scorecard SET notes = NULL WHERE interview_id IN
                         (SELECT id FROM hiring_interview WHERE application_id = ANY(%s::uuid[]))""", [app_ids])
            execute("""UPDATE notification_outbox SET to_email = NULL, body = ''
                        WHERE channel = 'email' AND payload->>'applicationId' = ANY(%s::text[])""", [app_ids])
        n += 1
    return n


def upsert_candidate(company_id: str, name: str, email: str, phone: str | None) -> str:
    """Find the candidate by e-mail (case-insensitive) or create them; existing details are kept and gaps filled."""
    r = one("""INSERT INTO hiring_candidate (company_id, name, email, phone) VALUES (%s, %s, lower(%s), %s)
               ON CONFLICT (company_id, lower(email)) WHERE email IS NOT NULL
               DO UPDATE SET phone = coalesce(hiring_candidate.phone, EXCLUDED.phone) RETURNING id""",
            [company_id, name, email, phone])
    return str(r['id'])


# ---------------------------------------------------------------- access

def _need_staff(user: AuthUser) -> None:
    if not has_role(user, 'manager', 'hr', 'recruiter'):
        raise ForbiddenError('requires role manager or hr or recruiter')


def _need_module(user: AuthUser) -> None:
    if not has_module(user, 'hiring'):
        raise ForbiddenError('requires role hr or recruiter')


def _s(v) -> str | None:
    return None if v is None else str(v)


def manager_unit(employee_id: str | None) -> str | None:
    if not employee_id:
        return None
    r = one('SELECT org_unit_id FROM employee_assignment WHERE employee_id = %s AND valid @> (now() AT TIME ZONE \'Asia/Kathmandu\')::date', [employee_id])
    return _s(r and r['org_unit_id'])


def can_see_job(user: AuthUser, job: dict) -> bool:
    """HR sees every job; a manager sees the jobs they manage, created, or that belong to their department."""
    if has_module(user, 'hiring'):
        return True
    if not has_role(user, 'manager'):
        return False
    if _s(job.get('created_by')) == user.id:
        return True
    if user.employee_id and _s(job.get('hiring_manager_id')) == str(user.employee_id):
        return True
    unit = manager_unit(user.employee_id)
    return bool(unit) and _s(job.get('org_unit_id')) == unit


def _check_refs(company_id: str, refs) -> None:
    for table, ref_id, label in refs:
        if not ref_id:
            continue
        if not one(f'SELECT 1 AS x FROM {table} WHERE id = %s AND company_id = %s', [ref_id, company_id]):
            raise BadRequestError(f'{label} does not exist in your company')


def _job_row(r: dict) -> dict:
    r['stages'] = r.get('stages') or []
    r['questions'] = r.get('questions') or []
    for k in ('id', 'company_id', 'requisition_id', 'org_unit_id', 'branch_id', 'hiring_manager_id', 'created_by'):
        if k in r:
            r[k] = _s(r[k])
    return r


def load_job(user: AuthUser, job_id: str) -> dict:
    job_id = parse_uuid(job_id)
    r = one('SELECT * FROM hiring_job WHERE id = %s AND company_id = %s', [job_id, company_id_of(user)])
    if not r or not can_see_job(user, _job_row(r)):
        raise NotFoundError('job not found')
    return r


def load_app(user: AuthUser, app_id: str, *, allow_panel: bool = False, lock: bool = False) -> tuple[dict, dict, str]:
    """An application with its job; HR and managers in scope get 'full' access, interview panel members 'panel'."""
    app_id = parse_uuid(app_id)
    a = one(f"""SELECT a.id, a.company_id, a.job_id, a.candidate_id, a.stage, a.status, a.cv_file_id, a.furthest_stage, a.created_at,
                       c.name, c.email, c.phone
                  FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id
                 WHERE a.id = %s AND a.company_id = %s{' FOR UPDATE OF a' if lock else ''}""", [app_id, company_id_of(user)])
    if not a:
        raise NotFoundError('application not found')
    for k in ('id', 'company_id', 'job_id', 'candidate_id', 'cv_file_id'):
        a[k] = _s(a[k])
    job = _job_row(one('SELECT * FROM hiring_job WHERE id = %s', [a['job_id']]))
    if can_see_job(user, job):
        return a, job, 'full'
    if allow_panel and one('SELECT 1 AS x FROM hiring_interview WHERE application_id = %s AND %s = ANY(panel) LIMIT 1', [app_id, user.id]):
        return a, job, 'panel'
    raise NotFoundError('application not found')


def _move_to(app: dict, job: dict, stage: str, actor_id: str) -> None:
    stages = job['stages']
    idx = stages.index(stage) if stage in stages else -1
    cur = stages.index(app['stage']) if app['stage'] in stages else -1
    if idx <= 0 or cur >= idx:
        return
    execute("""UPDATE hiring_application SET stage = %s, furthest_stage = greatest(furthest_stage, %s), stage_changed_at = now()
                WHERE id = %s""", [stage, idx, app['id']])
    add_event(company_id=app['company_id'], application_id=app['id'], actor_id=actor_id, kind='stage',
              body=f"{app['stage']} → {stage}", data={'from': app['stage'], 'to': stage})


# ---------------------------------------------------------------- requisitions

REQUISITION_BODY = Obj({
    'title': Str(trim=True, min=2, max=120), 'designation': _opt_text(120), 'orgUnitId': Uuid(nullish=True),
    'branchId': Uuid(nullish=True), 'headcount': Num(int_=True, min=1, max=500, default=1),
    'salaryMin': Money(nullish=True), 'salaryMax': Money(nullish=True), 'kind': Enum(('new', 'replacement'), default='new'),
    'replacingEmployeeId': Uuid(nullish=True), 'justification': Str(trim=True, min=10, max=4000),
})


def list_requisitions(user: AuthUser) -> list[dict]:
    _need_staff(user)
    company_id = company_id_of(user)
    hr = has_module(user, 'hiring')
    unit = None if hr else manager_unit(user.employee_id)
    return rows("""SELECT q.*, ou.name_en AS department, b.name_en AS branch, u.name AS raised_by_name, re.name_en AS replacing_name,
                          (SELECT count(*)::int FROM hiring_job j WHERE j.requisition_id = q.id) AS jobs
                     FROM hiring_requisition q LEFT JOIN org_unit ou ON ou.id = q.org_unit_id LEFT JOIN branch b ON b.id = q.branch_id
                     LEFT JOIN app_user u ON u.id = q.raised_by LEFT JOIN employee re ON re.id = q.replacing_employee_id
                    WHERE q.company_id = %s AND (%s OR q.raised_by = %s OR (q.org_unit_id IS NOT NULL AND q.org_unit_id = %s::uuid))
                    ORDER BY q.created_at DESC LIMIT 500""", [company_id, hr, user.id, unit])


def get_requisition(user: AuthUser, req_id: str) -> dict:
    req_id = parse_uuid(req_id)
    for r in list_requisitions(user):
        if str(r['id']) == req_id:
            return r
    raise NotFoundError('request not found')


def create_requisition(user: AuthUser, body: dict) -> str:
    _need_staff(user)
    company_id = company_id_of(user)
    b = parse(REQUISITION_BODY, body)
    if b.get('salaryMin') and b.get('salaryMax') and Decimal(b['salaryMax']) < Decimal(b['salaryMin']):
        raise BadRequestError('the highest salary cannot be below the lowest')
    if b['kind'] == 'replacement' and not b.get('replacingEmployeeId'):
        raise BadRequestError('choose the employee being replaced')
    _check_refs(company_id, [('org_unit', b.get('orgUnitId'), 'department'), ('branch', b.get('branchId'), 'branch'),
                             ('employee', b.get('replacingEmployeeId'), 'employee')])
    with transaction.atomic():
        r = one("""INSERT INTO hiring_requisition (company_id, raised_by, raised_by_employee_id, title, designation, org_unit_id, branch_id,
                          headcount, salary_min, salary_max, kind, replacing_employee_id, justification)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [company_id, user.id, user.employee_id, b['title'], b.get('designation'), b.get('orgUnitId'), b.get('branchId'),
                 b['headcount'], b.get('salaryMin'), b.get('salaryMax'), b['kind'],
                 b.get('replacingEmployeeId') if b['kind'] == 'replacement' else None, b['justification']])
        req_id = str(r['id'])
        start_workflow(company_id=company_id, object_type='job_requisition', object_id=req_id, employee_id=user.employee_id,
                       title='Hiring request', body=f"{user.name} asks to hire {b['headcount']} × {b['title']}.")
        audit('create', 'hiring_requisition', actor_id=user.id, row_id=req_id, after=b)
    return req_id


def cancel_requisition(user: AuthUser, req_id: str) -> None:
    _need_staff(user)
    req_id = parse_uuid(req_id)
    with transaction.atomic():
        q = one('SELECT raised_by, status FROM hiring_requisition WHERE id = %s AND company_id = %s FOR UPDATE',
                [req_id, company_id_of(user)])
        if not q:
            raise NotFoundError('request not found')
        if str(q['raised_by']) != user.id and not has_module(user, 'hiring'):
            raise NotFoundError('request not found')
        if q['status'] not in ('pending', 'approved'):
            raise ConflictError(f"this request is already {q['status']}")
        execute("UPDATE hiring_requisition SET status = 'cancelled', decided_at = coalesce(decided_at, now()) WHERE id = %s", [req_id])
        cancel_workflow('job_requisition', req_id)
        audit('cancel', 'hiring_requisition', actor_id=user.id, row_id=req_id)


# ---------------------------------------------------------------- jobs

JOB_COLS = """j.id, j.company_id, j.requisition_id, j.title, j.description, j.requirements, j.category, j.location, j.work_mode, j.employment_type,
  j.experience_min, j.experience_max, j.salary_min, j.salary_max, j.show_salary, j.openings, j.deadline, j.org_unit_id, j.branch_id, j.hiring_manager_id,
  j.stages, j.questions, j.status, j.published_at, j.closed_at, j.created_by, j.created_at, j.updated_at,
  ou.name_en AS department, hm.name_en AS hiring_manager_name"""


def _validate_job(b: dict) -> None:
    emin, emax = b.get('experienceMin'), b.get('experienceMax')
    if emin is not None and emax is not None and emax < emin:
        raise BadRequestError('the most experience cannot be below the least')
    if b.get('salaryMin') and b.get('salaryMax') and Decimal(b['salaryMax']) < Decimal(b['salaryMin']):
        raise BadRequestError('the highest salary cannot be below the lowest')
    qs = b.get('questions')
    if qs:
        ids = [q['id'] for q in qs]
        if len(set(ids)) != len(ids):
            raise BadRequestError('each question needs a different id')
        for q in qs:
            for rr in (q.get('knockout'), q.get('preferred')):
                if rr is None:
                    continue
                if q['kind'] == 'text':
                    raise BadRequestError(f'"{q["label"]}": text answers cannot be scored')
                if q['kind'] == 'yes_no' and not rr.get('equals'):
                    raise BadRequestError(f'"{q["label"]}": a yes or no rule needs a yes or no answer')
                if q['kind'] == 'number' and rr.get('min') is None:
                    raise BadRequestError(f'"{q["label"]}": a number rule needs a minimum')


def list_jobs(user: AuthUser, status: str | None = None) -> dict:
    _need_staff(user)
    company_id = company_id_of(user)
    q = parse(Obj({'status': Enum(('draft', 'open', 'closed'), optional=True)}), {'status': status} if status else {})
    hr = has_module(user, 'hiring')
    unit = None if hr else manager_unit(user.employee_id)
    jobs = rows(f"""SELECT {JOB_COLS},
                           (SELECT count(*)::int FROM hiring_application a WHERE a.job_id = j.id) AS applications,
                           (SELECT count(*)::int FROM hiring_application a WHERE a.job_id = j.id AND a.status = 'active') AS active,
                           (SELECT count(*)::int FROM hiring_application a WHERE a.job_id = j.id AND a.created_at > now() - interval '7 days') AS new_this_week,
                           (SELECT count(*)::int FROM hiring_application a WHERE a.job_id = j.id AND a.status = 'hired') AS hired
                      FROM hiring_job j LEFT JOIN org_unit ou ON ou.id = j.org_unit_id LEFT JOIN employee hm ON hm.id = j.hiring_manager_id
                     WHERE j.company_id = %s AND (%s::text IS NULL OR j.status = %s)
                       AND (%s OR j.created_by = %s OR j.hiring_manager_id = %s::uuid OR (j.org_unit_id IS NOT NULL AND j.org_unit_id = %s::uuid))
                     ORDER BY (j.status = 'open') DESC, j.created_at DESC LIMIT 500""",
                [company_id, q.get('status'), q.get('status'), hr, user.id, user.employee_id, unit])
    code = one('SELECT code FROM company WHERE id = %s', [company_id])
    return {'jobs': [_job_row(j) for j in jobs], 'companyCode': code and code['code']}


def create_job(user: AuthUser, body: dict) -> str:
    _need_module(user)
    company_id = company_id_of(user)
    b = parse(JOB_BODY, body)
    _validate_job(b)
    _check_refs(company_id, [('org_unit', b.get('orgUnitId'), 'department'), ('branch', b.get('branchId'), 'branch'),
                             ('employee', b.get('hiringManagerId'), 'hiring manager')])
    if b.get('requisitionId'):
        q = one('SELECT status FROM hiring_requisition WHERE id = %s AND company_id = %s', [b['requisitionId'], company_id])
        if not q:
            raise BadRequestError('hiring request does not exist in your company')
        if q['status'] != 'approved':
            raise BadRequestError('only an approved hiring request can be turned into a job')
    with transaction.atomic():
        r = one("""INSERT INTO hiring_job (company_id, requisition_id, title, description, requirements, category, location, work_mode,
                          employment_type, experience_min, experience_max, salary_min, salary_max, show_salary, openings, deadline,
                          org_unit_id, branch_id, hiring_manager_id, stages, questions, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [company_id, b.get('requisitionId'), b['title'], b['description'], b['requirements'], b.get('category'),
                 b.get('location'), b['workMode'], b['employmentType'], b.get('experienceMin'), b.get('experienceMax'),
                 b.get('salaryMin'), b.get('salaryMax'), b['showSalary'], b['openings'], b.get('deadline'), b.get('orgUnitId'),
                 b.get('branchId'), b.get('hiringManagerId'), json.dumps(b.get('stages') or DEFAULT_STAGES),
                 json.dumps(b.get('questions') or []), user.id])
        job_id = str(r['id'])
        audit('create', 'hiring_job', actor_id=user.id, row_id=job_id, after={'title': b['title']})
    return job_id


def get_job(user: AuthUser, job_id: str) -> dict:
    _need_staff(user)
    job = load_job(user, job_id)
    full = one(f"""SELECT {JOB_COLS} FROM hiring_job j LEFT JOIN org_unit ou ON ou.id = j.org_unit_id
                     LEFT JOIN employee hm ON hm.id = j.hiring_manager_id WHERE j.id = %s""", [job['id']])
    counts = rows("SELECT stage, count(*)::int AS n FROM hiring_application WHERE job_id = %s AND status = 'active' GROUP BY stage", [job['id']])
    return {'job': _job_row(full), 'stageCounts': {c['stage']: c['n'] for c in counts}}


_JOB_COL = {
    'title': 'title', 'description': 'description', 'requirements': 'requirements', 'category': 'category', 'location': 'location',
    'workMode': 'work_mode', 'employmentType': 'employment_type', 'experienceMin': 'experience_min', 'experienceMax': 'experience_max',
    'salaryMin': 'salary_min', 'salaryMax': 'salary_max', 'showSalary': 'show_salary', 'openings': 'openings', 'deadline': 'deadline',
    'orgUnitId': 'org_unit_id', 'branchId': 'branch_id', 'hiringManagerId': 'hiring_manager_id', 'stages': 'stages', 'questions': 'questions',
}


def update_job(user: AuthUser, job_id: str, body: dict) -> None:
    _need_module(user)
    company_id = company_id_of(user)
    job_id = parse_uuid(job_id)
    raw = parse(JOB_PATCH, body)
    # Only fields actually sent are changed (defaults must not overwrite stored values).
    b = {k: v for k, v in raw.items() if k in (body or {})}
    job = load_job(user, job_id)
    _validate_job({'experienceMin': None if job['experience_min'] is None else float(job['experience_min']),
                   'experienceMax': None if job['experience_max'] is None else float(job['experience_max']), **b})
    _check_refs(company_id, [('org_unit', b.get('orgUnitId'), 'department'), ('branch', b.get('branchId'), 'branch'),
                             ('employee', b.get('hiringManagerId'), 'hiring manager')])
    if 'requisitionId' in b:
        raise BadRequestError('the hiring request of a job cannot be changed')
    if b.get('stages'):
        used = [r['stage'] for r in rows('SELECT DISTINCT stage FROM hiring_application WHERE job_id = %s', [job_id])]
        missing = [s for s in used if s not in b['stages']]
        if missing:
            raise ConflictError(f"candidates are in: {', '.join(missing)}. Move them before removing the stage.")
    sets, vals = [], []
    for k, v in b.items():
        col = _JOB_COL.get(k)
        if not col:
            continue
        vals.append(json.dumps(v) if k in ('stages', 'questions') else v)
        sets.append(f'{col} = %s')
    if sets:
        with transaction.atomic():
            execute(f"UPDATE hiring_job SET {', '.join(sets)}, updated_at = now() WHERE id = %s", [*vals, job_id])
            audit('update', 'hiring_job', actor_id=user.id, row_id=job_id, after=b)


def publish_job(user: AuthUser, job_id: str) -> None:
    _need_module(user)
    job_id = parse_uuid(job_id)
    job = load_job(user, job_id)
    if job['status'] == 'open':
        return
    if job['deadline'] and job['deadline'].isoformat() < npt_today():
        raise BadRequestError('the application deadline has passed; set a new deadline first')
    if len((job['description'] or '').strip()) < 20:
        raise BadRequestError('add a job description (at least 20 characters) before publishing')
    with transaction.atomic():
        execute("""UPDATE hiring_job SET status = 'open', published_at = coalesce(published_at, now()), closed_at = NULL, updated_at = now()
                    WHERE id = %s""", [job_id])
        audit('publish', 'hiring_job', actor_id=user.id, row_id=job_id)


def close_job(user: AuthUser, job_id: str) -> None:
    _need_module(user)
    job_id = parse_uuid(job_id)
    load_job(user, job_id)
    with transaction.atomic():
        execute("UPDATE hiring_job SET status = 'closed', closed_at = now(), updated_at = now() WHERE id = %s", [job_id])
        audit('close', 'hiring_job', actor_id=user.id, row_id=job_id)


# ---------------------------------------------------------------- applications

def board(user: AuthUser, job_id: str, include: str = 'active') -> dict:
    _need_staff(user)
    job = load_job(user, job_id)
    q = parse(Obj({'include': Enum(('active', 'all'), default='active')}), {'include': include} if include else {})
    apps = rows("""SELECT a.id, a.stage, a.status, a.source, a.score, a.knocked_out, a.rating, a.tags, a.experience_years, a.current_employer,
                          a.created_at, a.stage_changed_at, a.rejected_reason, a.owner_user_id, ou.name AS owner_name, c.id AS candidate_id,
                          c.name, c.email, c.phone, (a.cv_file_id IS NOT NULL) AS has_cv,
                          (SELECT count(*)::int FROM hiring_application o WHERE o.candidate_id = a.candidate_id AND o.id <> a.id) AS other_applications,
                          (SELECT min(i.starts_at) FROM hiring_interview i WHERE i.application_id = a.id AND i.status = 'scheduled'
                              AND i.starts_at > now()) AS next_interview
                     FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id LEFT JOIN app_user ou ON ou.id = a.owner_user_id
                    WHERE a.job_id = %s AND (%s = 'all' OR a.status = 'active')
                    ORDER BY a.knocked_out, a.score DESC NULLS LAST, a.created_at""", [job['id'], q['include']])
    for a in apps:
        a['id'] = str(a['id'])
        a['tags'] = a['tags'] or []
    return {'applications': apps, 'stages': job['stages'], 'job': job}


ADD_CANDIDATE_BODY = Obj({
    'name': Str(trim=True, min=2, max=120), 'email': Str(email=True, max=200),
    'phone': Str(trim=True, regex=r'^[0-9+\- ()]{6,30}$', regex_message='phone number', nullish=True),
    'source': Enum(SOURCES, default='manual'), 'cvFileId': Uuid(nullish=True), 'coverNote': _opt_text(4000),
    'experienceYears': Num(min=0, max=60, nullish=True), 'currentEmployer': _opt_text(120),
    'answers': Record(S(), optional=True),
})


def add_candidate(user: AuthUser, job_id: str, body: dict) -> str:
    _need_module(user)
    company_id = company_id_of(user)
    job = load_job(user, job_id)
    b = parse(ADD_CANDIDATE_BODY, body)
    answers = clean_answers(job['questions'], b.get('answers'))
    with transaction.atomic():
        cand = upsert_candidate(company_id, b['name'], b['email'], b.get('phone'))
        if one('SELECT 1 AS x FROM hiring_application WHERE job_id = %s AND candidate_id = %s', [job['id'], cand]):
            raise ConflictError('this candidate has already applied for this job')
        score = score_application(job, b.get('experienceYears'), answers)
        r = one("""INSERT INTO hiring_application (company_id, job_id, candidate_id, stage, source, cover_note, answers, experience_years,
                          current_employer, score, score_detail, knocked_out, owner_user_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [company_id, job['id'], cand, job['stages'][0], b['source'], b.get('coverNote'), json.dumps(answers),
                 b.get('experienceYears'), b.get('currentEmployer'), score['score'], json.dumps(score['detail']),
                 score['knockedOut'], user.id])
        app_id = str(r['id'])
        if b.get('cvFileId'):
            attach_file(company_id=company_id, file_id=b['cvFileId'], user_id=user.id, purpose='hiring_cv',
                        attached_to=f'hiring_application:{app_id}')
            execute('UPDATE hiring_application SET cv_file_id = %s WHERE id = %s', [b['cvFileId'], app_id])
        add_event(company_id=company_id, application_id=app_id, actor_id=user.id, kind='applied',
                  body=f"Added by {user.name} ({b['source'].replace('_', ' ', 1)})")
    return app_id


def application_detail(user: AuthUser, app_id: str) -> dict:
    app, job, access = load_app(user, app_id, allow_panel=True)
    full = one("""SELECT a.id, a.job_id, a.candidate_id, a.stage, a.status, a.source, a.cover_note, a.answers, a.experience_years,
                         a.current_employer, a.score, a.score_detail, a.knocked_out, a.rating, a.tags, a.owner_user_id, a.rejected_reason,
                         a.closed_at, a.hired_employee_id, a.consent_at, a.stage_changed_at, a.created_at,
                         (a.cv_file_id IS NOT NULL) AS has_cv, c.name, c.email, c.phone, c.purged_at, ou.name AS owner_name
                    FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id LEFT JOIN app_user ou ON ou.id = a.owner_user_id
                   WHERE a.id = %s""", [app['id']])
    full['id'] = str(full['id'])
    full['tags'] = full['tags'] or []
    full['answers'] = full['answers'] or {}
    full['score_detail'] = full['score_detail'] or []
    is_full = access == 'full'
    others = rows("""SELECT a.id, a.stage, a.status, a.created_at, j.title FROM hiring_application a JOIN hiring_job j ON j.id = a.job_id
                      WHERE a.candidate_id = %s AND a.id <> %s ORDER BY a.created_at DESC""", [app['candidate_id'], app['id']]) if is_full else []
    events = rows("""SELECT e.id, e.kind, e.body, e.data, e.created_at, u.name AS actor FROM hiring_event e LEFT JOIN app_user u ON u.id = e.actor_id
                      WHERE e.application_id = %s ORDER BY e.created_at DESC LIMIT 300""", [app['id']]) if is_full else []
    interviews = rows("""SELECT i.id, i.title, i.starts_at, i.duration_min, i.mode, i.location, i.panel, i.status,
                                (SELECT array_agg(u.name ORDER BY u.name) FROM app_user u WHERE u.id = ANY(i.panel)) AS panel_names
                           FROM hiring_interview i WHERE i.application_id = %s ORDER BY i.starts_at""", [app['id']])
    for i in interviews:
        i['id'] = str(i['id'])
        i['panel'] = [str(p) for p in (i['panel'] or [])]
    cards = rows("""SELECT s.id, s.interview_id, s.reviewer_id, s.ratings, s.recommendation, s.notes, s.submitted_at, u.name AS reviewer
                      FROM hiring_scorecard s JOIN app_user u ON u.id = s.reviewer_id WHERE s.interview_id = ANY(%s::uuid[])
                     ORDER BY s.submitted_at""", [[i['id'] for i in interviews]])
    for c in cards:
        c['interview_id'], c['reviewer_id'] = str(c['interview_id']), str(c['reviewer_id'])
    scorecards = cards if is_full else [c for c in cards if c['reviewer_id'] == user.id]
    offers = rows("""SELECT id, designation, salary, join_date, expires_on, notes, status, created_at, decided_at, responded_at
                       FROM hiring_offer WHERE application_id = %s ORDER BY created_at DESC""", [app['id']]) if is_full else []
    for o in offers:
        o['id'] = str(o['id'])
    return {'application': full, 'access': access, 'otherApplications': others, 'events': events, 'interviews': interviews,
            'scorecards': scorecards, 'offers': offers,
            'job': {'id': job['id'], 'title': job['title'], 'stages': job['stages'], 'questions': job['questions'],
                    'status': job['status'], 'requisitionId': job['requisition_id']}}


def cv_file(user: AuthUser, app_id: str) -> dict:
    app, _, _ = load_app(user, app_id, allow_panel=True)
    if not app['cv_file_id']:
        raise NotFoundError('no CV on file')
    return read_file(app['company_id'], app['cv_file_id'])


UPDATE_APP_BODY = Obj({
    'rating': Num(int_=True, min=1, max=5, nullish=True), 'tags': Arr(Str(trim=True, min=1, max=30), max=10, optional=True),
    'ownerUserId': Uuid(nullish=True),
})


def update_application(user: AuthUser, app_id: str, body: dict) -> None:
    _need_staff(user)
    b = parse(UPDATE_APP_BODY, body)
    app, _, _ = load_app(user, app_id)
    if b.get('ownerUserId'):
        if not one("SELECT 1 AS x FROM app_user WHERE id = %s AND company_id = %s AND status = 'active'", [b['ownerUserId'], app['company_id']]):
            raise BadRequestError('owner must be an active user in your company')
    with transaction.atomic():
        if 'rating' in b:
            execute('UPDATE hiring_application SET rating = %s WHERE id = %s', [b['rating'], app['id']])
            add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='rating',
                      body=f"Rated {b['rating']} of 5" if b['rating'] else 'Rating cleared')
        if 'tags' in b:
            execute('UPDATE hiring_application SET tags = %s WHERE id = %s', [list(dict.fromkeys(b['tags'])), app['id']])
        if 'ownerUserId' in b:
            execute('UPDATE hiring_application SET owner_user_id = %s WHERE id = %s', [b['ownerUserId'], app['id']])


def move_application(user: AuthUser, app_id: str, body: dict) -> None:
    _need_staff(user)
    stage = parse(Obj({'stage': Str(trim=True, min=1, max=40)}), body)['stage']
    with transaction.atomic():
        app, job, _ = load_app(user, app_id, lock=True)
        if app['status'] != 'active':
            raise ConflictError(f"this application is {app['status']}")
        stages = job['stages']
        idx = stages.index(stage) if stage in stages else -1
        if idx < 0:
            raise BadRequestError('that stage is not part of this job')
        if idx == len(stages) - 1:
            raise BadRequestError('use "Hire" to move a candidate to Hired once they accept an offer')
        if stage == app['stage']:
            return
        execute("""UPDATE hiring_application SET stage = %s, furthest_stage = greatest(furthest_stage, %s), stage_changed_at = now()
                    WHERE id = %s""", [stage, idx, app['id']])
        add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='stage',
                  body=f"{app['stage']} → {stage}", data={'from': app['stage'], 'to': stage})


REJECT_BODY = Obj({'reason': Str(trim=True, min=3, max=500), 'notifyCandidate': Bool(default=True)})


def reject_application(user: AuthUser, app_id: str, body: dict) -> bool:
    _need_staff(user)
    b = parse(REJECT_BODY, body)
    with transaction.atomic():
        app, job, _ = load_app(user, app_id, lock=True)
        if app['status'] != 'active':
            raise ConflictError(f"this application is already {app['status']}")
        execute("UPDATE hiring_application SET status = 'rejected', rejected_reason = %s, closed_at = now() WHERE id = %s",
                [b['reason'], app['id']])
        add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='rejected', body=b['reason'])
        opened = rows("""UPDATE hiring_offer SET status = 'withdrawn' WHERE application_id = %s
                          AND status IN ('pending_approval', 'approved', 'accepted') RETURNING id""", [app['id']])
        for o in opened:
            cancel_workflow('job_offer', str(o['id']))
        if not b['notifyCandidate']:
            return False
        company = company_name(app['company_id'])
        return email_candidate(
            company_id=app['company_id'], application_id=app['id'], to=app['email'], actor_id=user.id, template='hiring.rejected',
            title=f"Your application for {job['title']}",
            body=(f"Dear {app['name']},\n\nThank you for your interest in the {job['title']} role at {company} and for the time you put "
                  f"into your application. After careful consideration we will not be moving forward with your application this time."
                  f"\n\nWe wish you every success.\n\n{company}"))


def reopen_application(user: AuthUser, app_id: str) -> None:
    _need_module(user)
    with transaction.atomic():
        app, _, _ = load_app(user, app_id, lock=True)
        if app['status'] not in ('rejected', 'withdrawn'):
            raise ConflictError(f"this application is {app['status']}")
        c = one('SELECT purged_at FROM hiring_candidate WHERE id = %s', [app['candidate_id']])
        if c and c['purged_at']:
            raise ConflictError("this candidate's data has been removed")
        execute("UPDATE hiring_application SET status = 'active', rejected_reason = NULL, closed_at = NULL WHERE id = %s", [app['id']])
        add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='reopened', body='Application reopened')


def add_note(user: AuthUser, app_id: str, body: dict) -> None:
    _need_staff(user)
    text = parse(Obj({'body': Str(trim=True, min=1, max=4000)}), body)['body']
    app, _, _ = load_app(user, app_id)
    add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='note', body=text)


def export_candidate(user: AuthUser, app_id: str) -> dict:
    """Everything held about a candidate, for a data access request. HR only, audited."""
    _need_module(user)
    app, _, _ = load_app(user, app_id)
    cand = one('SELECT id, name, email, phone, created_at, purged_at FROM hiring_candidate WHERE id = %s', [app['candidate_id']])
    apps = rows("""SELECT a.id, j.title AS job, a.stage, a.status, a.source, a.cover_note, a.answers, a.experience_years, a.current_employer,
                          a.score, a.score_detail, a.rating, a.tags, a.rejected_reason, a.created_at, a.closed_at, a.consent_at
                     FROM hiring_application a JOIN hiring_job j ON j.id = a.job_id WHERE a.candidate_id = %s ORDER BY a.created_at""",
                [app['candidate_id']])
    ids = [str(a['id']) for a in apps]
    events = rows('SELECT application_id, kind, body, created_at FROM hiring_event WHERE application_id = ANY(%s::uuid[]) ORDER BY created_at', [ids])
    interviews = rows('SELECT application_id, title, starts_at, mode, status FROM hiring_interview WHERE application_id = ANY(%s::uuid[])', [ids])
    offers = rows('SELECT application_id, designation, salary, join_date, status, created_at FROM hiring_offer WHERE application_id = ANY(%s::uuid[])', [ids])
    audit('export', 'hiring_candidate', actor_id=user.id, row_id=app['candidate_id'])
    return {'exportedAt': datetime.now(timezone.utc), 'candidate': cand, 'applications': apps, 'events': events,
            'interviews': interviews, 'offers': offers, '_candidate_id': app['candidate_id']}


# ---------------------------------------------------------------- interviews

INTERVIEW_BODY = Obj({
    'title': Str(trim=True, min=2, max=80, default='Interview'), 'date': IsoDate(), 'time': Hhmm(),
    'durationMin': Num(int_=True, min=10, max=480, default=45), 'mode': Enum(('in_person', 'phone', 'video')),
    'location': _opt_text(300), 'panel': Arr(Uuid(), min=1, max=10), 'notifyCandidate': Bool(default=True),
})


def npt_instant(d: str, t: str) -> datetime:
    """A Nepal wall-clock date and time as an instant."""
    return datetime.fromisoformat(f'{d}T{t}:00').replace(tzinfo=NPT)


def _check_panel(company_id: str, panel: list[str]) -> list[str]:
    ids = list(dict.fromkeys(panel))
    found = rows("SELECT id FROM app_user WHERE id = ANY(%s::uuid[]) AND company_id = %s AND status = 'active'", [ids, company_id])
    if len(found) != len(ids):
        raise BadRequestError('every panel member must be an active user in your company')
    return ids


MODE_HOW = {'video': 'by video', 'phone': 'by phone'}


def schedule_interview(user: AuthUser, app_id: str, body: dict) -> str:
    _need_staff(user)
    b = parse(INTERVIEW_BODY, body)
    starts_at = npt_instant(b['date'], b['time'])
    if starts_at < datetime.now(timezone.utc) - timedelta(hours=1):
        raise BadRequestError('the interview time is in the past')
    with transaction.atomic():
        app, job, _ = load_app(user, app_id, lock=True)
        if app['status'] != 'active':
            raise ConflictError(f"this application is {app['status']}")
        panel = _check_panel(app['company_id'], b['panel'])
        r = one("""INSERT INTO hiring_interview (company_id, application_id, title, starts_at, duration_min, mode, location, panel, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::uuid[], %s) RETURNING id""",
                [app['company_id'], app['id'], b['title'], starts_at, b['durationMin'], b['mode'], b.get('location'), panel, user.id])
        interview_id = str(r['id'])
        starts_iso = _iso_z(starts_at)
        add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='interview',
                  body=f"{b['title']} scheduled", data={'interviewId': interview_id, 'startsAt': starts_iso})
        _move_to(app, job, 'Interview', user.id)
        for uid in panel:
            notify(company_id=app['company_id'], user_id=uid, template='hiring.interview', title=f"Interview: {app['name']}",
                   body=f"You are on the interview panel for {app['name']} ({job['title']}).",
                   payload={'applicationId': app['id'], 'interviewId': interview_id, 'startsAt': starts_iso}, channels=('push',))
        if b['notifyCandidate']:
            company = company_name(app['company_id'])
            loc = b.get('location')
            if b['mode'] == 'video':
                how = f"by video{f' ({loc})' if loc else ''}"
            elif b['mode'] == 'phone':
                how = 'by phone'
            else:
                how = f"in person{f' at {loc}' if loc else ''}"
            email_candidate(
                company_id=app['company_id'], application_id=app['id'], to=app['email'], actor_id=user.id,
                template='hiring.interview_invite', title=f"Interview for {job['title']} at {company}",
                body=(f"Dear {app['name']},\n\nWe would like to invite you to an interview for the {job['title']} role on "
                      f"{bs_label(b['date'])} ({b['date']}) at {b['time']} Nepal time, {how}. It will take about {b['durationMin']} minutes."
                      f"\n\nPlease reply to let us know if this time does not work for you.\n\n{company}"))
    return interview_id


def _iso_z(d: datetime) -> str:
    """Date.toISOString(): UTC with milliseconds."""
    d = d.astimezone(timezone.utc)
    return d.strftime('%Y-%m-%dT%H:%M:%S.') + f'{d.microsecond // 1000:03d}Z'


INTERVIEWS_QUERY = Obj({'scope': Enum(('mine', 'all'), default='mine'), 'from': IsoDate(optional=True), 'to': IsoDate(optional=True)})


def list_interviews(user: AuthUser, query: dict) -> list[dict]:
    company_id = company_id_of(user)
    q = parse(INTERVIEWS_QUERY, {k: v for k, v in (query or {}).items() if v not in (None, '')})
    if q['scope'] == 'all' and not has_role(user, 'manager', 'hr', 'recruiter'):
        raise ForbiddenError('only managers and HR can see all interviews')
    hr = has_module(user, 'hiring')
    unit = None if hr else manager_unit(user.employee_id)
    out = rows("""SELECT i.id, i.application_id, i.title, i.starts_at, i.duration_min, i.mode, i.location, i.status, i.panel,
                         (SELECT array_agg(u.name ORDER BY u.name) FROM app_user u WHERE u.id = ANY(i.panel)) AS panel_names,
                         c.name AS candidate, j.title AS job, j.id AS job_id, (%(me)s = ANY(i.panel)) AS on_panel,
                         EXISTS (SELECT 1 FROM hiring_scorecard s WHERE s.interview_id = i.id AND s.reviewer_id = %(me)s) AS my_scorecard,
                         (SELECT count(*)::int FROM hiring_scorecard s WHERE s.interview_id = i.id) AS scorecards
                    FROM hiring_interview i JOIN hiring_application a ON a.id = i.application_id JOIN hiring_candidate c ON c.id = a.candidate_id
                    JOIN hiring_job j ON j.id = a.job_id
                   WHERE i.company_id = %(co)s
                     AND (%(from)s::date IS NULL OR i.starts_at >= (%(from)s::date)::timestamp AT TIME ZONE 'Asia/Kathmandu')
                     AND (%(to)s::date IS NULL OR i.starts_at < (%(to)s::date + 1)::timestamp AT TIME ZONE 'Asia/Kathmandu')
                     AND (CASE WHEN %(scope)s = 'mine' THEN %(me)s = ANY(i.panel)
                               ELSE (%(hr)s OR %(me)s = ANY(i.panel) OR j.created_by = %(me)s OR j.hiring_manager_id = %(emp)s::uuid
                                     OR (j.org_unit_id IS NOT NULL AND j.org_unit_id = %(unit)s::uuid)) END)
                   ORDER BY i.starts_at LIMIT 500""",
               {'co': company_id, 'me': user.id, 'from': q.get('from'), 'to': q.get('to'), 'scope': q['scope'], 'hr': hr,
                'emp': user.employee_id, 'unit': unit})
    for i in out:
        i['id'], i['application_id'] = str(i['id']), str(i['application_id'])
        i['panel'] = [str(p) for p in (i['panel'] or [])]
    return out


INTERVIEW_PATCH = Obj({
    'status': Enum(('scheduled', 'completed', 'cancelled'), optional=True), 'date': IsoDate(optional=True), 'time': Hhmm(optional=True),
    'location': _opt_text(300), 'panel': Arr(Uuid(), min=1, max=10, optional=True),
})


def update_interview(user: AuthUser, interview_id: str, body: dict) -> None:
    _need_staff(user)
    b = parse(INTERVIEW_PATCH, body)
    interview_id = parse_uuid(interview_id)
    with transaction.atomic():
        i = one('SELECT application_id, starts_at FROM hiring_interview WHERE id = %s AND company_id = %s FOR UPDATE',
                [interview_id, company_id_of(user)])
        if not i:
            raise NotFoundError('interview not found')
        app, _, _ = load_app(user, str(i['application_id']))
        sets, vals = [], []
        if b.get('status'):
            sets.append('status = %s')
            vals.append(b['status'])
        if b.get('date') or b.get('time'):
            cur = i['starts_at'].astimezone(NPT)
            sets.append('starts_at = %s')
            vals.append(npt_instant(b.get('date') or cur.date().isoformat(), b.get('time') or cur.strftime('%H:%M')))
        if 'location' in b:
            sets.append('location = %s')
            vals.append(b['location'])
        if b.get('panel'):
            sets.append('panel = %s::uuid[]')
            vals.append(_check_panel(app['company_id'], b['panel']))
        if not sets:
            return
        execute(f"UPDATE hiring_interview SET {', '.join(sets)} WHERE id = %s", [*vals, interview_id])
        add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='interview',
                  data={'interviewId': interview_id},
                  body='Interview cancelled' if b.get('status') == 'cancelled' else 'Interview completed'
                  if b.get('status') == 'completed' else 'Interview updated')


class _Ratings(Record):
    def check(self, v, path):
        out = super().check(v, path)
        if not (1 <= len(out) <= 10):
            _fail(path, 'rate between 1 and 10 criteria')
        return out


SCORECARD_BODY = Obj({
    'ratings': _Ratings(Num(int_=True, min=1, max=5)),
    'recommendation': Enum(RECOMMENDATIONS), 'notes': _opt_text(4000),
})


def _check_rating_keys(ratings: dict) -> None:
    from hajiri.core.errors import ValidationError
    bad = [k for k in ratings if not (2 <= len(k.strip()) <= 40)]
    if bad:
        raise ValidationError([{'path': f'ratings.{bad[0]}', 'message': 'Invalid key in record'}])


def submit_scorecard(user: AuthUser, interview_id: str, body: dict) -> None:
    """Panel members score the interview they sat on; one scorecard each, editable until the candidate is hired."""
    b = parse(SCORECARD_BODY, body)
    _check_rating_keys(b['ratings'])
    ratings = {k.strip(): v for k, v in b['ratings'].items()}
    interview_id = parse_uuid(interview_id)
    company_id = company_id_of(user)
    with transaction.atomic():
        i = one('SELECT application_id, panel, status FROM hiring_interview WHERE id = %s AND company_id = %s', [interview_id, company_id])
        if not i or user.id not in [str(p) for p in (i['panel'] or [])]:
            raise NotFoundError('interview not found')
        if i['status'] == 'cancelled':
            raise ConflictError('this interview was cancelled')
        st = one('SELECT status FROM hiring_application WHERE id = %s', [i['application_id']])
        if st and st['status'] == 'hired':
            raise ConflictError('this candidate has already been hired')
        execute("""INSERT INTO hiring_scorecard (interview_id, reviewer_id, ratings, recommendation, notes) VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (interview_id, reviewer_id) DO UPDATE SET ratings = EXCLUDED.ratings, recommendation = EXCLUDED.recommendation,
                          notes = EXCLUDED.notes, submitted_at = now()""",
                [interview_id, user.id, json.dumps(ratings), b['recommendation'], b.get('notes')])
        add_event(company_id=company_id, application_id=str(i['application_id']), actor_id=user.id, kind='scorecard',
                  body=f"Scorecard: {b['recommendation'].replace('_', ' ', 1)}", data={'interviewId': interview_id})


def interview_for_scorecard(user: AuthUser, interview_id: str) -> dict:
    """The interview a panel member is about to score, with their saved scorecard if any."""
    interview_id = parse_uuid(interview_id)
    i = one("""SELECT i.id, i.title, i.panel, i.status, i.application_id, c.name AS candidate FROM hiring_interview i
                 JOIN hiring_application a ON a.id = i.application_id JOIN hiring_candidate c ON c.id = a.candidate_id
                WHERE i.id = %s AND i.company_id = %s""", [interview_id, company_id_of(user)])
    if not i or user.id not in [str(p) for p in (i['panel'] or [])]:
        raise NotFoundError('interview not found')
    mine = one('SELECT ratings, recommendation, notes FROM hiring_scorecard WHERE interview_id = %s AND reviewer_id = %s',
               [interview_id, user.id])
    return {**i, 'id': str(i['id']), 'mine': mine}


# ---------------------------------------------------------------- offers

OFFER_BODY = Obj({
    'designation': Str(trim=True, min=2, max=120),
    'salary': Money(refine=(lambda s: Decimal(s) > 0, 'salary must be above zero')),
    'joinDate': IsoDate(), 'expiresOn': IsoDate(nullish=True), 'notes': _opt_text(4000),
})


def create_offer(user: AuthUser, app_id: str, body: dict) -> str:
    _need_module(user)
    b = parse(OFFER_BODY, body)
    if b['joinDate'] < npt_today():
        raise BadRequestError('the joining date is in the past')
    if b.get('expiresOn') and b['expiresOn'] > b['joinDate']:
        raise BadRequestError('the offer should expire on or before the joining date')
    with transaction.atomic():
        app, job, _ = load_app(user, app_id, lock=True)
        if app['status'] != 'active':
            raise ConflictError(f"this application is {app['status']}")
        if one("SELECT 1 AS x FROM hiring_offer WHERE application_id = %s AND status IN ('pending_approval', 'approved', 'accepted')", [app['id']]):
            raise ConflictError('this candidate already has an open offer; withdraw it first')
        r = one("""INSERT INTO hiring_offer (company_id, application_id, designation, salary, join_date, expires_on, notes, created_by,
                          created_by_employee_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [app['company_id'], app['id'], b['designation'], b['salary'], b['joinDate'], b.get('expiresOn'), b.get('notes'),
                 user.id, user.employee_id])
        offer_id = str(r['id'])
        # Recording the drafter as the subject means a drafter who is also an employee cannot approve their own offer.
        start_workflow(company_id=app['company_id'], object_type='job_offer', object_id=offer_id, employee_id=user.employee_id,
                       title='Offer needs approval', body=f"Offer for {app['name']} as {b['designation']} ({job['title']}).")
        _move_to(app, job, 'Offer', user.id)
        add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='offer',
                  body=f"Offer drafted: {b['designation']}", data={'offerId': offer_id})
        audit('create', 'hiring_offer', actor_id=user.id, row_id=offer_id, after=b)
    return offer_id


def _load_offer(user: AuthUser, offer_id: str, lock: bool = False) -> tuple[dict, dict, dict]:
    offer_id = parse_uuid(offer_id)
    o = one(f"SELECT * FROM hiring_offer WHERE id = %s AND company_id = %s{' FOR UPDATE' if lock else ''}", [offer_id, company_id_of(user)])
    if not o:
        raise NotFoundError('offer not found')
    o['id'] = str(o['id'])
    app, job, _ = load_app(user, str(o['application_id']))
    return o, app, job


def offer_letter(user: AuthUser, offer_id: str) -> str:
    _need_staff(user)
    o, app, job = _load_offer(user, offer_id)
    extra = one("""SELECT ou.name_en AS department, j.location, j.employment_type, co.code, co.name_en FROM hiring_job j
                     LEFT JOIN org_unit ou ON ou.id = j.org_unit_id JOIN company co ON co.id = j.company_id WHERE j.id = %s""", [job['id']])
    return render_offer_letter(
        company=extra['name_en'], candidate=app['name'], designation=o['designation'], department=extra['department'],
        location=extra['location'], employment_type=extra['employment_type'], salary=o['salary'], join_date=o['join_date'],
        expires_on=o['expires_on'], notes=o['notes'], issued_on=o['created_at'].astimezone(NPT).date().isoformat(),
        reference=f"{extra['code']}/HR/OFFER/{o['id'][-6:].upper()}", draft=o['status'] not in ('approved', 'accepted'))


def respond_offer(user: AuthUser, offer_id: str, body: dict) -> None:
    _need_module(user)
    response = parse(Obj({'response': Enum(('accepted', 'declined'))}), body)['response']
    with transaction.atomic():
        o, app, _ = _load_offer(user, offer_id, lock=True)
        if o['status'] != 'approved':
            raise ConflictError('the offer must be approved before the candidate can respond' if o['status'] == 'pending_approval'
                                else f"this offer is {o['status'].replace('_', ' ', 1)}")
        execute('UPDATE hiring_offer SET status = %s, responded_at = now() WHERE id = %s', [response, o['id']])
        add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='offer',
                  body=f'Offer {response} by the candidate', data={'offerId': o['id']})
        audit(response, 'hiring_offer', actor_id=user.id, row_id=o['id'])


def withdraw_offer(user: AuthUser, offer_id: str) -> None:
    _need_module(user)
    with transaction.atomic():
        o, app, _ = _load_offer(user, offer_id, lock=True)
        if o['status'] not in OPEN_OFFER:
            raise ConflictError(f"this offer is {o['status'].replace('_', ' ', 1)}")
        if app['status'] == 'hired':
            raise ConflictError('the candidate has already been hired')
        execute("UPDATE hiring_offer SET status = 'withdrawn' WHERE id = %s", [o['id']])
        cancel_workflow('job_offer', o['id'])
        add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='offer', body='Offer withdrawn',
                  data={'offerId': o['id']})
        audit('withdraw', 'hiring_offer', actor_id=user.id, row_id=o['id'])


# ---------------------------------------------------------------- hire

HIRE_BODY = Obj({
    'empCode': Str(trim=True, min=1, max=32), 'joinDate': IsoDate(optional=True), 'designation': Str(trim=True, max=120, optional=True),
    'branchId': Uuid(optional=True), 'orgUnitId': Uuid(nullish=True), 'managerId': Uuid(nullish=True), 'shiftId': Uuid(optional=True),
    'employmentType': Enum(('regular', 'work_based', 'time_based', 'casual', 'part_time'), default='regular'),
    'gender': Enum(('male', 'female', 'other'), optional=True), 'probationEnd': IsoDate(nullish=True),
})


def hire(user: AuthUser, app_id: str, body: dict) -> str:
    """
    Turn a candidate who accepted an offer into an employee; the CV stays linked through the application. The employee
    row, its assignment and shift are written with SQL exactly as the TS route does (no people service is involved);
    the career-timeline and onboarding steps run through hajiri.integrations.hooks.run_hire_hooks.
    """
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')
    b = parse(HIRE_BODY, body)
    company_id = company_id_of(user)
    _check_refs(company_id, [('branch', b.get('branchId'), 'branch'), ('org_unit', b.get('orgUnitId'), 'department'),
                             ('employee', b.get('managerId'), 'manager'), ('shift', b.get('shiftId'), 'shift')])
    with transaction.atomic():
        app, job, _ = load_app(user, app_id, lock=True)
        if app['status'] != 'active':
            raise ConflictError(f"this application is {app['status']}")
        offer = one("""SELECT id, designation, join_date FROM hiring_offer WHERE application_id = %s AND status = 'accepted'
                        ORDER BY created_at DESC LIMIT 1""", [app['id']])
        if not offer:
            raise ConflictError('the candidate must accept an approved offer before they can be hired')
        if one('SELECT 1 AS x FROM employee WHERE company_id = %s AND emp_code = %s', [company_id, b['empCode']]):
            raise ConflictError('this employee code is already in use')
        join_date = b.get('joinDate') or offer['join_date'].isoformat()
        if b.get('probationEnd') and b['probationEnd'] < join_date:
            raise BadRequestError('probation end cannot be before join date')
        emp = one("""INSERT INTO employee (company_id, emp_code, name_en, gender, join_date, designation, phone, email, probation_end,
                            weekly_off_days, regime)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '{6}', 'ssf') RETURNING id""",
                  [company_id, b['empCode'], app['name'], b.get('gender'), join_date, b.get('designation') or offer['designation'],
                   app['phone'], app['email'], b.get('probationEnd')])
        emp_id = str(emp['id'])
        refs = one('SELECT branch_id FROM hiring_job WHERE id = %s', [job['id']])
        execute("""INSERT INTO employee_assignment (employee_id, valid, branch_id, org_unit_id, manager_id, employment_type)
                   VALUES (%s, daterange(%s, NULL), %s, %s, %s, %s)""",
                [emp_id, join_date, b.get('branchId') or (refs and refs['branch_id']),
                 b['orgUnitId'] if 'orgUnitId' in b else job['org_unit_id'],
                 b['managerId'] if 'managerId' in b else job['hiring_manager_id'], b['employmentType']])
        if b.get('shiftId'):
            execute('INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES (%s, %s, daterange(%s, NULL))',
                    [emp_id, b['shiftId'], join_date])
        stages = job['stages']
        execute("""UPDATE hiring_application SET status = 'hired', stage = %s, furthest_stage = %s, hired_employee_id = %s, closed_at = now(),
                          stage_changed_at = now() WHERE id = %s""", [stages[-1], len(stages) - 1, emp_id, app['id']])
        if job['requisition_id']:
            execute("""UPDATE hiring_requisition SET filled = filled + 1,
                              status = CASE WHEN filled + 1 >= headcount THEN 'filled' ELSE status END WHERE id = %s""", [job['requisition_id']])
        add_event(company_id=company_id, application_id=app['id'], actor_id=user.id, kind='hired', body=f"Hired as {b['empCode']}",
                  data={'employeeId': emp_id})
        audit('hire', 'employee', actor_id=user.id, row_id=emp_id,
              after={'applicationId': app['id'], 'empCode': b['empCode'], 'joinDate': join_date})
        run_hire_hooks(HireEvent(company_id=company_id, employee_id=emp_id, application_id=app['id'], job_id=job['id'],
                                 join_date=join_date, actor_id=user.id))
    return emp_id


# ---------------------------------------------------------------- lookups, reports

def lookups(user: AuthUser) -> dict:
    """Names for the hiring forms: users who can sit on a panel or own a candidate, departments, branches and shifts."""
    _need_staff(user)
    company_id = company_id_of(user)
    return {
        'users': rows("SELECT id, name, roles FROM app_user WHERE company_id = %s AND status = 'active' ORDER BY name", [company_id]),
        'orgUnits': rows('SELECT id, name_en, kind FROM org_unit WHERE company_id = %s ORDER BY name_en', [company_id]),
        'branches': rows('SELECT id, name_en FROM branch WHERE company_id = %s ORDER BY name_en', [company_id]),
        'shifts': rows('SELECT id, code, name_en FROM shift WHERE company_id = %s ORDER BY name_en', [company_id]),
    }


def reports(user: AuthUser, query: dict | None = None) -> dict:
    _need_module(user)
    company_id = company_id_of(user)
    q = parse(Obj({'from': IsoDate(optional=True), 'to': IsoDate(optional=True)}),
              {k: v for k, v in (query or {}).items() if v not in (None, '')})
    p = {'co': company_id, 'from': q.get('from'), 'to': q.get('to')}
    window = """a.company_id = %(co)s AND (%(from)s::date IS NULL OR a.created_at >= (%(from)s::date)::timestamp AT TIME ZONE 'Asia/Kathmandu')
                AND (%(to)s::date IS NULL OR a.created_at < (%(to)s::date + 1)::timestamp AT TIME ZONE 'Asia/Kathmandu')"""
    jobs = rows(f"""SELECT j.id, j.title, j.status, j.stages FROM hiring_job j WHERE j.company_id = %(co)s
                     AND EXISTS (SELECT 1 FROM hiring_application a WHERE a.job_id = j.id AND {window}) ORDER BY j.created_at DESC""", p)
    reached = rows(f'SELECT a.job_id, a.furthest_stage, a.status FROM hiring_application a WHERE {window}', p)
    funnel = []
    for j in jobs:
        apps = [x for x in reached if x['job_id'] == j['id']]
        funnel.append({'jobId': str(j['id']), 'title': j['title'], 'status': j['status'], 'total': len(apps),
                       'rejected': sum(1 for a in apps if a['status'] == 'rejected'),
                       'stages': [{'stage': s, 'reached': sum(1 for a in apps if a['furthest_stage'] >= i)}
                                  for i, s in enumerate(j['stages'] or [])]})
    tth = one(f"""SELECT round(avg(extract(epoch FROM a.closed_at - a.created_at) / 86400)::numeric, 1)::text AS avg,
                         round((percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM a.closed_at - a.created_at) / 86400))::numeric, 1)::text AS median,
                         count(*)::int AS hires
                    FROM hiring_application a WHERE {window} AND a.status = 'hired'""", p)
    sources = rows(f"""SELECT a.source, count(*)::int AS applications, count(*) FILTER (WHERE a.status = 'hired')::int AS hired
                         FROM hiring_application a WHERE {window} GROUP BY a.source ORDER BY 2 DESC""", p)
    totals = one(f"""SELECT count(*)::int AS applications, count(*) FILTER (WHERE a.status = 'active')::int AS active,
                            count(*) FILTER (WHERE a.status = 'hired')::int AS hired, count(*) FILTER (WHERE a.status = 'rejected')::int AS rejected
                       FROM hiring_application a WHERE {window}""", p)

    def num(v):
        return None if v is None else _norm_num(float(v))
    return {'totals': totals, 'funnel': funnel, 'sources': sources,
            'timeToHire': {'averageDays': num(tth and tth['avg']), 'medianDays': num(tth and tth['median']),
                           'hires': (tth and tth['hires']) or 0}}


def policy(user: AuthUser) -> dict:
    _need_staff(user)
    return hiring_policy(company_id_of(user))


def careers_url(request_base: str, job_id: str | None = None, code: str | None = None) -> str:
    from urllib.parse import quote
    base = f"{request_base}/careers{f'/{job_id}' if job_id else ''}"
    return f'{base}?company={quote(code, safe="")}' if code else base


