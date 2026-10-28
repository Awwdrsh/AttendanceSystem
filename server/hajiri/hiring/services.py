"""
Hiring and applicant tracking (port of apps/api/src/modules/hiring/service.ts and routes.ts): requisitions approved
through the workflow engine, job openings with screening questions and a transparent score, the candidate pipeline,
interviews and scorecards, offers approved by a second HR user, hire-into-employee, reports, the retention policy
and the nightly purge of candidates who were not selected.

Bodies use the TS camelCase shape and are validated with the serializers in hajiri.hiring.serializers, so error
messages match the TS API. Every function takes the signed-in AuthUser and works in its ACTIVE company: rows of
another company are simply not found. People who can sit on a panel, own a candidate or hear about applications are
the people of the company (home accounts, members of the group with access and group-role holders).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db import IntegrityError, transaction
from django.db.backends.postgresql.psycopg_any import DateRange
from django.db.models import (
    Aggregate, Avg, BooleanField, Case, Count, DecimalField, Exists, ExpressionWrapper, F, FloatField, Func, IntegerField,
    OuterRef, Q, Subquery, TextField, Value, When,
)
from django.db.models.fields.json import KT
from django.db.models.functions import Coalesce, Extract, Greatest, Lower, Now

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_module, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import NPT, npt_today
from hajiri.core.collation import collate_key
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.settings_store import add_setting_if_missing, get_setting, put_setting
from hajiri.core.users import company_users, user_ids_with_role
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (
    AppUser, Branch, Company, Employee, EmployeeAssignment, HiringApplication, HiringCandidate, HiringEvent, HiringInterview,
    HiringJob, HiringOffer, HiringRequisition, HiringScorecard, NotificationOutbox, OrgUnit, Shift, ShiftAssignment,
)
from hajiri.files.store import attach_file, delete_file, read_file
from hajiri.integrations.hooks import HireEvent, on_candidate_hired, run_hire_hooks  # noqa: F401  (re-exported)
from hajiri.notifications.service import notify, user_id_for_employee
from hajiri.workflow.service import DecisionContext, cancel_workflow, register_handler, register_summariser, start_workflow

from . import serializers as S
from .offer_letter import render_offer_letter
from .serializers import DEFAULT_STAGES, EMPLOYMENT_TYPES, RECOMMENDATIONS, SOURCES, WORK_MODES  # noqa: F401 (public names)

App, Job, Offer, Req, Iv = HiringApplication, HiringJob, HiringOffer, HiringRequisition, HiringInterview
OPEN_OFFER = (Offer.Status.PENDING_APPROVAL, Offer.Status.APPROVED, Offer.Status.ACCEPTED)
CLOSED_APP = (App.Status.REJECTED, App.Status.WITHDRAWN)
POLICY_KEY = 'hiring.policy'
DEFAULT_POLICY = {'retentionDays': 180, 'careersEnabled': True}


# ---------------------------------------------------------------- small query helpers

def _values(qs, **cols) -> list[dict]:
    """The rows of `qs` as dicts with exactly these keys; each value is a field path ('candidate__name') or an
    expression. (values() cannot name a key after a model field, e.g. 'job' for the job title.)"""
    ann = {f'v_{k}': (F(v) if isinstance(v, str) else v) for k, v in cols.items()}
    return [{k: r[f'v_{k}'] for k in cols} for r in qs.annotate(**ann).values(*ann)]


def _columns(model) -> dict[str, str]:
    """Every column of a table as {column name: attribute}, the keys of SELECT * (created_by, not created_by_id)."""
    return {f.column: f.attname for f in model._meta.concrete_fields}


def count_of(qs) -> Coalesce:
    """A correlated `(SELECT count(*) ...)` for an annotation (no GROUP BY on the outer query)."""
    return Coalesce(Subquery(qs.order_by().annotate(n=Func(F('pk'), function='COUNT', output_field=IntegerField()))
                             .values('n')[:1]), 0)


def _flag(q: Q) -> ExpressionWrapper:
    return ExpressionWrapper(q, output_field=BooleanField())


class Median(Aggregate):
    """percentile_cont(0.5) WITHIN GROUP (ORDER BY ...): PostgreSQL's median, which Django has no aggregate for."""
    function = 'percentile_cont'
    template = '%(function)s(0.5) WITHIN GROUP (ORDER BY %(expressions)s)'
    output_field = FloatField()


def _s(v) -> str | None:
    return None if v is None else str(v)


def _num_text(v) -> str:
    """3 not 3.0, 2.5 as 2.5 (how the TS API printed numbers in notes)."""
    return format(Decimal(str(v)).normalize(), 'f')


def _half_up(x) -> int:
    """Math.round for the non-negative values here: halves go up."""
    return int(Decimal(str(x)).quantize(Decimal(1), ROUND_HALF_UP))


def _iso(at: datetime) -> str:
    """An instant as JSON shows it: UTC with milliseconds."""
    return at.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def npt_instant(d: str, t: str) -> datetime:
    """A Nepal wall-clock date and time as an instant."""
    return datetime.fromisoformat(f'{d}T{t}:00').replace(tzinfo=NPT)


def _npt_day_start(d: str) -> datetime:
    return datetime.combine(datetime.fromisoformat(d).date(), time(0), tzinfo=NPT)


def _checked(serializer, body) -> dict:
    return plain(validate(serializer, body if body is not None else {}))


# ---------------------------------------------------------------- screening score

class _Missing:
    def __repr__(self):
        return 'MISSING'


MISSING = _Missing()


def _as_yes_no(a) -> str | None:
    if a is True or a == 'yes':
        return 'yes'
    if a is False or a == 'no':
        return 'no'
    return None


def _as_number(a) -> float | None:
    """A numeric answer as a float: numbers as they are, true/false as 1/0, numeric text parsed (blank text is 0, as
    the TS form's Number('') was); None when it is not a finite number."""
    if a is None or a is MISSING:
        return None
    if isinstance(a, bool):
        return 1.0 if a else 0.0
    if isinstance(a, (int, float, Decimal)):
        n = float(a)
    else:
        s = str(a).strip()
        try:
            n = float(s) if s else 0.0
        except ValueError:
            return None
    return n if n == n and abs(n) != float('inf') else None


def _meets(rule: dict, a) -> bool:
    if rule.get('equals'):
        return _as_yes_no(a) == rule['equals']
    if rule.get('min') is not None:
        n = _as_number(a)
        return n is not None and n >= rule['min']
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
            points = _half_up(40 * y / mn) if mn > 0 else 40
            note = f'{_num_text(y)} years, below the {_num_text(mn)} years asked for'
        elif mx is not None and y > mx:
            points = 30
            note = f'{_num_text(y)} years, more than the {_num_text(mx)} years in the range'
        else:
            points = 40
            note = f'{_num_text(y)} years, within the range'
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
    return {'score': _half_up(100 * earned / total) if total > 0 else None, 'knockedOut': knocked_out, 'detail': detail}


def _answer_text(v) -> str:
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, float):
        return _num_text(v)
    return str(v)


def clean_answers(questions: list[dict], raw: dict | None) -> dict:
    """Check answers against the job's questions: required ones present, types right, unknown ids dropped."""
    out: dict = {}
    src = raw or {}
    for q in questions:
        v = src.get(q['id'])
        if v is None or (isinstance(v, str) and v.strip() == ''):
            if q.get('required', True):
                raise BadRequestError('Please answer every required question.')
            continue
        if q['kind'] == 'yes_no':
            yn = _as_yes_no(v)
            if not yn:
                raise BadRequestError('Answer the yes or no questions with yes or no.')
            out[q['id']] = yn
        elif q['kind'] == 'number':
            n = _as_number(v)
            if n is None or n < 0 or n > 1_000_000:
                raise BadRequestError('Number answers must be a number.')
            out[q['id']] = int(n) if n.is_integer() else n
        else:
            out[q['id']] = _answer_text(v).strip()[:1000]
    return out


# ---------------------------------------------------------------- timeline, e-mail, notifications

def add_event(*, company_id: str, application_id: str, actor_id: str | None, kind: str, body: str | None = None,
              data: dict | None = None) -> None:
    HiringEvent.objects.create(company_id=company_id, application_id=application_id, actor_id=actor_id, kind=kind, body=body,
                               data=plain(data or {}))


def email_candidate(*, company_id: str, application_id: str, to: str | None, template: str, title: str, body: str,
                    actor_id: str | None) -> bool:
    """Queue an e-mail to a candidate through the outbox (they are not users, so the address rides on the row) and log it."""
    if not to:
        return False
    NotificationOutbox.objects.create(
        company_id=company_id, user_id=None, employee_id=None, channel=NotificationOutbox.Channel.EMAIL, template=template,
        title=title, body=body, payload={'applicationId': str(application_id)}, status=NotificationOutbox.Status.QUEUED,
        to_email=to)
    add_event(company_id=company_id, application_id=application_id, actor_id=actor_id, kind='email', body=title,
              data={'template': template})
    return True


def company_name(company_id: str) -> str:
    return Company.objects.for_company(company_id).values_list('name_en', flat=True).first() or 'our company'


def job_watchers(company_id: str, hiring_manager_id: str | None) -> list[str]:
    """HR of the company (members of the group with HR access too) plus the job's hiring manager: who hears about
    new applications."""
    ids = list(dict.fromkeys(user_ids_with_role(company_id, 'hr')))
    if hiring_manager_id:
        u = user_id_for_employee(str(hiring_manager_id))
        if u and u not in ids:
            ids.append(u)
    return ids


# ---------------------------------------------------------------- policy

def hiring_policy(company_id: str) -> dict:
    v = get_setting(company_id, POLICY_KEY, {})
    if not isinstance(v, dict):
        v = {}
    rd = v.get('retentionDays')
    return {'retentionDays': rd if isinstance(rd, (int, float)) and not isinstance(rd, bool) else 180,
            'careersEnabled': v.get('careersEnabled') is not False}


def seed_policy(company_id: str) -> bool:
    """The default policy for a new company (180 days' retention, careers page on); True when this call added it."""
    return add_setting_if_missing(company_id, POLICY_KEY, dict(DEFAULT_POLICY))


def set_policy(user: AuthUser, body: dict) -> dict:
    _need_module(user)
    company_id = company_id_of(user)
    b = _checked(S.PolicyIn, body)
    with transaction.atomic():
        put_setting(company_id, POLICY_KEY, b, updated_by=user.id)
        audit('update', 'company_setting', actor_id=user.id, row_id=None, after={'key': POLICY_KEY, **b})
    return b


# ---------------------------------------------------------------- workflow handlers

def _decide_requisition(object_id: str, state: str, ctx: DecisionContext) -> None:
    q = Req.objects.select_for_update().filter(pk=object_id).first()
    if not q or q.status != Req.Status.PENDING:
        return
    if state == 'approved' and str(ctx.actor_id) == str(q.raised_by_id):
        raise ForbiddenError('you raised this hiring request; someone else must approve it')
    Req.objects.filter(pk=q.pk).update(status=state, decided_at=Now(), decision_reason=ctx.reason)
    notify(company_id=str(q.company_id), user_id=str(q.raised_by_id), template=f'job_requisition.{state}',
           title='Hiring request approved' if state == 'approved' else 'Hiring request rejected',
           body=f'Your request to hire "{q.title}" was {state}{f": {ctx.reason}" if ctx.reason else ""}.',
           payload={'requisitionId': str(object_id)})


def _decide_offer(object_id: str, state: str, ctx: DecisionContext) -> None:
    o = Offer.objects.select_for_update(of=('self',)).select_related('application__candidate').filter(pk=object_id).first()
    if not o or o.status != Offer.Status.PENDING_APPROVAL:
        return
    if state == 'approved' and o.created_by_id is not None and str(ctx.actor_id) == str(o.created_by_id):
        raise ForbiddenError('you drafted this offer; someone else must approve it')
    Offer.objects.filter(pk=o.pk).update(status=state, decided_at=Now())
    reason = f': {ctx.reason}' if ctx.reason else ''
    add_event(company_id=str(o.company_id), application_id=str(o.application_id), actor_id=ctx.actor_id, kind='offer',
              body=f'Offer {state}{reason}')
    if o.created_by_id:
        notify(company_id=str(o.company_id), user_id=str(o.created_by_id), template=f'job_offer.{state}',
               title='Offer approved' if state == 'approved' else 'Offer rejected',
               body=f'The offer for {o.application.candidate.name} was {state}{reason}.',
               payload={'offerId': str(object_id), 'applicationId': str(o.application_id)})


def _summarise_requisition(inst: dict) -> dict:
    found = _values(Req.objects.filter(pk=inst['object_id']), title='title', designation='designation', headcount='headcount',
                    kind='kind', salary_min='salary_min', salary_max='salary_max', justification='justification',
                    department='org_unit__name_en')
    return found[0] if found else {}


def _summarise_offer(inst: dict) -> dict:
    found = _values(Offer.objects.filter(pk=inst['object_id']), designation='designation', salary='salary',
                    join_date='join_date', candidate='application__candidate__name', job='application__job__title')
    return found[0] if found else {}


def register_workflow() -> None:
    register_handler('job_requisition', _decide_requisition)
    register_handler('job_offer', _decide_offer)
    register_summariser('job_requisition', _summarise_requisition)
    register_summariser('job_offer', _summarise_offer)


register_workflow()


# ---------------------------------------------------------------- retention

def _purge_due(company_id: str, cutoff: datetime):
    """Candidates of a company whose every application closed (rejected or withdrawn) before `cutoff`."""
    apps = App.objects.filter(candidate=OuterRef('pk'))
    still_held = apps.annotate(last=Coalesce('closed_at', 'created_at')).filter(~Q(status__in=CLOSED_APP) | Q(last__gt=cutoff))
    return (HiringCandidate.objects.for_company(company_id).filter(purged_at=None)
            .filter(Exists(apps)).exclude(Exists(still_held)).values_list('id', flat=True))


def purge_rejected_candidates(now: datetime | None = None) -> int:
    """
    Daily: once a candidate has had no open application for the retention period (rejected or withdrawn), their name,
    contact details, CVs, cover notes, answers and notes are removed. Counts and stage history stay for reports.
    Each company keeps candidates for its own retention period.
    """
    now = now or datetime.now(timezone.utc)
    n = 0
    companies = HiringCandidate.objects.filter(purged_at=None).order_by().values_list('company_id', flat=True).distinct()
    for co in [str(c) for c in companies]:
        cutoff = now - timedelta(days=int(hiring_policy(co)['retentionDays']))
        for cand_id in [str(c) for c in _purge_due(co, cutoff)]:
            with transaction.atomic():
                apps = App.objects.for_company(co).filter(candidate_id=cand_id)
                files = list(apps.values_list('id', 'cv_file_id'))
                for _, file_id in files:
                    if file_id:
                        delete_file(co, str(file_id))
                HiringCandidate.objects.filter(pk=cand_id).update(name='Removed candidate', email=None, phone=None, purged_at=now)
                apps.update(cv_file=None, cover_note=None, answers={}, current_employer=None, tags=[], score_detail=[])
                app_ids = [a for a, _ in files]
                (HiringEvent.objects.for_company(co).filter(application_id__in=app_ids, kind__in=('note', 'email', 'rejected', 'applied'))
                 .update(body=None))
                HiringScorecard.objects.filter(interview__application_id__in=app_ids).update(notes=None)
                (NotificationOutbox.objects.for_company(co).filter(channel=NotificationOutbox.Channel.EMAIL)
                 .alias(app=KT('payload__applicationId')).filter(app__in=[str(a) for a in app_ids])
                 .update(to_email=None, body=''))
            n += 1
    return n


def upsert_candidate(company_id: str, name: str, email: str, phone: str | None) -> str:
    """Find the candidate by e-mail (case-insensitive) or create them; existing details are kept and gaps filled."""
    email = email.lower()
    same = HiringCandidate.objects.for_company(company_id).alias(e=Lower('email')).filter(e=email)
    cand_id = same.values_list('id', flat=True).first()
    if cand_id is None:
        try:
            with transaction.atomic():   # a savepoint: a parallel application with the same address may win the insert
                return str(HiringCandidate.objects.create(company_id=company_id, name=name, email=email, phone=phone).id)
        except IntegrityError:
            cand_id = same.values_list('id', flat=True).get()
    if phone:
        HiringCandidate.objects.filter(pk=cand_id, phone=None).update(phone=phone)
    return str(cand_id)


# ---------------------------------------------------------------- access

def _need_staff(user: AuthUser) -> None:
    if not has_role(user, 'manager', 'hr', 'recruiter'):
        raise ForbiddenError('requires role manager or hr or recruiter')


def _need_module(user: AuthUser) -> None:
    if not has_module(user, 'hiring'):
        raise ForbiddenError('requires role hr or recruiter')


def manager_unit(employee_id: str | None, company_id: str | None = None) -> str | None:
    """The department of an employee today (their current assignment)."""
    if not employee_id:
        return None
    qs = EmployeeAssignment.objects.filter(employee_id=employee_id, valid__contains=npt_today())
    if company_id:
        qs = qs.for_company(company_id)
    return _s(qs.values_list('org_unit_id', flat=True).first())


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
    unit = manager_unit(user.employee_id, user.company_id)
    return bool(unit) and _s(job.get('org_unit_id')) == unit


def _in_scope(user: AuthUser, *, created_by: str, org_unit: str, hiring_manager: str | None = None) -> Q | None:
    """The rows a manager may see: theirs, their department's, or (jobs) the ones they manage. None for HR, who sees
    everything."""
    if has_module(user, 'hiring'):
        return None
    q = Q(**{created_by: user.id})
    if hiring_manager and user.employee_id:
        q |= Q(**{hiring_manager: user.employee_id})
    unit = manager_unit(user.employee_id, user.company_id)
    if unit:
        q |= Q(**{org_unit: unit})
    return q


REF_MODELS = {'org_unit': OrgUnit, 'branch': Branch, 'employee': Employee, 'shift': Shift}


def _check_refs(company_id: str, refs) -> None:
    """Ids from the body must be rows of the active company."""
    for table, ref_id, label in refs:
        if ref_id and not REF_MODELS[table].objects.for_company(company_id).filter(pk=ref_id).exists():
            raise BadRequestError(f'{label} does not exist in your company')


def _job_row(r: dict) -> dict:
    r['stages'] = r.get('stages') or []
    r['questions'] = r.get('questions') or []
    for k in ('id', 'company_id', 'requisition_id', 'org_unit_id', 'branch_id', 'hiring_manager_id', 'created_by'):
        if k in r:
            r[k] = _s(r[k])
    return r


def _job_rows(qs, **extra) -> list[dict]:
    return [_job_row(r) for r in _values(qs, **_columns(Job), **extra)]


def load_job(user: AuthUser, job_id: str) -> dict:
    job_id = parse_uuid(job_id)
    found = _job_rows(Job.objects.for_company(company_id_of(user)).filter(pk=job_id))
    if not found or not can_see_job(user, found[0]):
        raise NotFoundError('job not found')
    return found[0]


def _company_user_ids(company_id: str) -> set[str]:
    return {str(u['id']) for u in company_users(company_id)}


def load_app(user: AuthUser, app_id: str, *, allow_panel: bool = False, lock: bool = False) -> tuple[dict, dict, str]:
    """An application with its job; HR and managers in scope get 'full' access, interview panel members 'panel'."""
    app_id = parse_uuid(app_id)
    qs = App.objects.for_company(company_id_of(user)).filter(pk=app_id)
    if lock:
        qs = qs.select_for_update(of=('self',))
    found = _values(qs, id='id', company_id='company_id', job_id='job_id', candidate_id='candidate_id', stage='stage',
                    status='status', cv_file_id='cv_file_id', furthest_stage='furthest_stage', created_at='created_at',
                    name='candidate__name', email='candidate__email', phone='candidate__phone')
    if not found:
        raise NotFoundError('application not found')
    a = found[0]
    for k in ('id', 'company_id', 'job_id', 'candidate_id', 'cv_file_id'):
        a[k] = _s(a[k])
    job = _job_rows(Job.objects.for_company(a['company_id']).filter(pk=a['job_id']))[0]
    if can_see_job(user, job):
        return a, job, 'full'
    if allow_panel and Iv.objects.filter(application_id=app_id, panel__contains=[user.id]).exists():
        return a, job, 'panel'
    raise NotFoundError('application not found')


def _set_stage(app: dict, stage: str, idx: int, actor_id: str) -> None:
    App.objects.filter(pk=app['id']).update(stage=stage, furthest_stage=Greatest('furthest_stage', Value(idx)),
                                            stage_changed_at=Now())
    add_event(company_id=app['company_id'], application_id=app['id'], actor_id=actor_id, kind='stage',
              body=f"{app['stage']} → {stage}", data={'from': app['stage'], 'to': stage})


def _move_to(app: dict, job: dict, stage: str, actor_id: str) -> None:
    """Move forward to a stage when the job has it and the candidate is not past it already."""
    stages = job['stages']
    idx = stages.index(stage) if stage in stages else -1
    cur = stages.index(app['stage']) if app['stage'] in stages else -1
    if idx <= 0 or cur >= idx:
        return
    _set_stage(app, stage, idx, actor_id)


# ---------------------------------------------------------------- requisitions

def _requisitions(user: AuthUser):
    _need_staff(user)
    qs = Req.objects.for_company(company_id_of(user))
    scope = _in_scope(user, created_by='raised_by_id', org_unit='org_unit_id')
    return (qs if scope is None else qs.filter(scope)).order_by('-created_at')


def _requisition_rows(qs) -> list[dict]:
    return _values(qs, **_columns(Req), department='org_unit__name_en', branch='branch__name_en', raised_by_name='raised_by__name',
                   replacing_name='replacing_employee__name_en',
                   jobs=count_of(Job.objects.filter(requisition=OuterRef('pk'))))


def list_requisitions(user: AuthUser) -> list[dict]:
    return _requisition_rows(_requisitions(user)[:500])


def get_requisition(user: AuthUser, req_id: str) -> dict:
    req_id = parse_uuid(req_id)
    found = _requisition_rows(_requisitions(user).filter(pk=req_id))
    if not found:
        raise NotFoundError('request not found')
    return found[0]


def create_requisition(user: AuthUser, body: dict) -> str:
    _need_staff(user)
    company_id = company_id_of(user)
    b = _checked(S.RequisitionIn, body)
    if b.get('salaryMin') and b.get('salaryMax') and Decimal(b['salaryMax']) < Decimal(b['salaryMin']):
        raise BadRequestError('the highest salary cannot be below the lowest')
    if b['kind'] == Req.Kind.REPLACEMENT and not b.get('replacingEmployeeId'):
        raise BadRequestError('choose the employee being replaced')
    _check_refs(company_id, [('org_unit', b.get('orgUnitId'), 'department'), ('branch', b.get('branchId'), 'branch'),
                             ('employee', b.get('replacingEmployeeId'), 'employee')])
    with transaction.atomic():
        r = Req.objects.create(
            company_id=company_id, raised_by_id=user.id, raised_by_employee_id=user.employee_id, title=b['title'],
            designation=b.get('designation'), org_unit_id=b.get('orgUnitId'), branch_id=b.get('branchId'),
            headcount=b['headcount'], salary_min=b.get('salaryMin'), salary_max=b.get('salaryMax'), kind=b['kind'],
            replacing_employee_id=b.get('replacingEmployeeId') if b['kind'] == Req.Kind.REPLACEMENT else None,
            justification=b['justification'])
        req_id = str(r.id)
        start_workflow(company_id=company_id, object_type='job_requisition', object_id=req_id, employee_id=user.employee_id,
                       title='Hiring request', body=f"{user.name} asks to hire {b['headcount']} × {b['title']}.")
        audit('create', 'hiring_requisition', actor_id=user.id, row_id=req_id, after=b)
    return req_id


def cancel_requisition(user: AuthUser, req_id: str) -> None:
    _need_staff(user)
    req_id = parse_uuid(req_id)
    with transaction.atomic():
        q = Req.objects.for_company(company_id_of(user)).select_for_update().filter(pk=req_id).first()
        if not q or (str(q.raised_by_id) != user.id and not has_module(user, 'hiring')):
            raise NotFoundError('request not found')
        if q.status not in (Req.Status.PENDING, Req.Status.APPROVED):
            raise ConflictError(f'this request is already {q.status}')
        Req.objects.filter(pk=q.pk).update(status=Req.Status.CANCELLED, decided_at=Coalesce('decided_at', Now()))
        cancel_workflow('job_requisition', req_id)
        audit('cancel', 'hiring_requisition', actor_id=user.id, row_id=req_id)


# ---------------------------------------------------------------- jobs

JOB_NAMES = {'department': 'org_unit__name_en', 'hiring_manager_name': 'hiring_manager__name_en'}


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


def jobs_queryset(user: AuthUser, status: str | None = None):
    """The jobs this person may see in the active company, open ones first, newest first."""
    _need_staff(user)
    q = _checked(S.JobsQueryIn, {'status': status} if status else {})
    qs = Job.objects.for_company(company_id_of(user))
    scope = _in_scope(user, created_by='created_by_id', hiring_manager='hiring_manager_id', org_unit='org_unit_id')
    if scope is not None:
        qs = qs.filter(scope)
    if q.get('status'):
        qs = qs.filter(status=q['status'])
    return qs.order_by(Case(When(status=Job.Status.OPEN, then=0), default=1), '-created_at')


def list_jobs(user: AuthUser, status: str | None = None) -> dict:
    company_id = company_id_of(user)
    apps = App.objects.filter(job=OuterRef('pk'))
    jobs = _job_rows(jobs_queryset(user, status)[:500], **JOB_NAMES,
                     applications=count_of(apps), active=count_of(apps.filter(status=App.Status.ACTIVE)),
                     new_this_week=count_of(apps.filter(created_at__gt=Now() - timedelta(days=7))),
                     hired=count_of(apps.filter(status=App.Status.HIRED)))
    code = Company.objects.for_company(company_id).values_list('code', flat=True).first()
    return {'jobs': jobs, 'companyCode': code}


def create_job(user: AuthUser, body: dict) -> str:
    _need_module(user)
    company_id = company_id_of(user)
    b = _checked(S.JobIn, body)
    _validate_job(b)
    _check_refs(company_id, [('org_unit', b.get('orgUnitId'), 'department'), ('branch', b.get('branchId'), 'branch'),
                             ('employee', b.get('hiringManagerId'), 'hiring manager')])
    if b.get('requisitionId'):
        status = Req.objects.for_company(company_id).filter(pk=b['requisitionId']).values_list('status', flat=True).first()
        if status is None:
            raise BadRequestError('hiring request does not exist in your company')
        if status != Req.Status.APPROVED:
            raise BadRequestError('only an approved hiring request can be turned into a job')
    with transaction.atomic():
        job = Job.objects.create(
            company_id=company_id, requisition_id=b.get('requisitionId'), title=b['title'], description=b['description'],
            requirements=b['requirements'], category=b.get('category'), location=b.get('location'), work_mode=b['workMode'],
            employment_type=b['employmentType'], experience_min=b.get('experienceMin'), experience_max=b.get('experienceMax'),
            salary_min=b.get('salaryMin'), salary_max=b.get('salaryMax'), show_salary=b['showSalary'], openings=b['openings'],
            deadline=b.get('deadline'), org_unit_id=b.get('orgUnitId'), branch_id=b.get('branchId'),
            hiring_manager_id=b.get('hiringManagerId'), stages=b.get('stages') or DEFAULT_STAGES,
            questions=b.get('questions') or [], created_by_id=user.id)
        job_id = str(job.id)
        audit('create', 'hiring_job', actor_id=user.id, row_id=job_id, after={'title': b['title']})
    return job_id


def get_job(user: AuthUser, job_id: str) -> dict:
    _need_staff(user)
    job = load_job(user, job_id)
    full = _job_rows(Job.objects.for_company(job['company_id']).filter(pk=job['id']), **JOB_NAMES)[0]
    counts = (App.objects.for_company(job['company_id']).filter(job_id=job['id'], status=App.Status.ACTIVE)
              .values('stage').annotate(n=Count('id')).values_list('stage', 'n'))
    return {'job': full, 'stageCounts': dict(counts)}


_JOB_FIELD = {
    'title': 'title', 'description': 'description', 'requirements': 'requirements', 'category': 'category', 'location': 'location',
    'workMode': 'work_mode', 'employmentType': 'employment_type', 'experienceMin': 'experience_min', 'experienceMax': 'experience_max',
    'salaryMin': 'salary_min', 'salaryMax': 'salary_max', 'showSalary': 'show_salary', 'openings': 'openings', 'deadline': 'deadline',
    'orgUnitId': 'org_unit_id', 'branchId': 'branch_id', 'hiringManagerId': 'hiring_manager_id', 'stages': 'stages',
    'questions': 'questions',
}


def update_job(user: AuthUser, job_id: str, body: dict) -> None:
    _need_module(user)
    company_id = company_id_of(user)
    job_id = parse_uuid(job_id)
    # Only fields actually sent are changed (the patch serializer has no defaults).
    b = _checked(S.JobPatchIn, body)
    job = load_job(user, job_id)
    _validate_job({'experienceMin': None if job['experience_min'] is None else float(job['experience_min']),
                   'experienceMax': None if job['experience_max'] is None else float(job['experience_max']), **b})
    _check_refs(company_id, [('org_unit', b.get('orgUnitId'), 'department'), ('branch', b.get('branchId'), 'branch'),
                             ('employee', b.get('hiringManagerId'), 'hiring manager')])
    if 'requisitionId' in b:
        raise BadRequestError('the hiring request of a job cannot be changed')
    if b.get('stages'):
        used = App.objects.for_company(company_id).filter(job_id=job_id).order_by('stage').values_list('stage', flat=True).distinct()
        missing = [s for s in used if s not in b['stages']]
        if missing:
            raise ConflictError(f"candidates are in: {', '.join(missing)}. Move them before removing the stage.")
    changes = {_JOB_FIELD[k]: v for k, v in b.items() if k in _JOB_FIELD}
    if changes:
        with transaction.atomic():
            Job.objects.filter(pk=job_id).update(**changes, updated_at=Now())
            audit('update', 'hiring_job', actor_id=user.id, row_id=job_id, after=b)


def publish_job(user: AuthUser, job_id: str) -> None:
    _need_module(user)
    job_id = parse_uuid(job_id)
    job = load_job(user, job_id)
    if job['status'] == Job.Status.OPEN:
        return
    if job['deadline'] and job['deadline'] < npt_today():
        raise BadRequestError('the application deadline has passed; set a new deadline first')
    if len((job['description'] or '').strip()) < 20:
        raise BadRequestError('add a job description (at least 20 characters) before publishing')
    with transaction.atomic():
        Job.objects.filter(pk=job_id).update(status=Job.Status.OPEN, published_at=Coalesce('published_at', Now()), closed_at=None,
                                             updated_at=Now())
        audit('publish', 'hiring_job', actor_id=user.id, row_id=job_id)


def close_job(user: AuthUser, job_id: str) -> None:
    _need_module(user)
    job_id = parse_uuid(job_id)
    load_job(user, job_id)
    with transaction.atomic():
        Job.objects.filter(pk=job_id).update(status=Job.Status.CLOSED, closed_at=Now(), updated_at=Now())
        audit('close', 'hiring_job', actor_id=user.id, row_id=job_id)


# ---------------------------------------------------------------- applications

def board(user: AuthUser, job_id: str, include: str = 'active') -> dict:
    _need_staff(user)
    job = load_job(user, job_id)
    q = _checked(S.BoardQueryIn, {'include': include} if include else {})
    qs = App.objects.for_company(job['company_id']).filter(job_id=job['id'])
    if q['include'] != 'all':
        qs = qs.filter(status=App.Status.ACTIVE)
    qs = qs.order_by('knocked_out', F('score').desc(nulls_last=True), 'created_at')
    upcoming = Iv.objects.filter(application=OuterRef('pk'), status=Iv.Status.SCHEDULED, starts_at__gt=Now()).order_by('starts_at')
    apps = _values(
        qs, id='id', stage='stage', status='status', source='source', score='score', knocked_out='knocked_out', rating='rating',
        tags='tags', experience_years='experience_years', current_employer='current_employer', created_at='created_at',
        stage_changed_at='stage_changed_at', rejected_reason='rejected_reason', owner_user_id='owner_user_id',
        owner_name='owner_user__name', candidate_id='candidate_id', name='candidate__name', email='candidate__email',
        phone='candidate__phone', has_cv=_flag(Q(cv_file__isnull=False)),
        other_applications=count_of(App.objects.filter(candidate=OuterRef('candidate_id')).exclude(pk=OuterRef('pk'))),
        next_interview=Subquery(upcoming.values('starts_at')[:1]))
    for a in apps:
        a['id'] = str(a['id'])
        a['tags'] = a['tags'] or []
    return {'applications': apps, 'stages': job['stages'], 'job': job}


def _decimal(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def add_candidate(user: AuthUser, job_id: str, body: dict) -> str:
    _need_module(user)
    company_id = company_id_of(user)
    job = load_job(user, job_id)
    b = _checked(S.AddCandidateIn, body)
    answers = clean_answers(job['questions'], b.get('answers'))
    with transaction.atomic():
        cand = upsert_candidate(company_id, b['name'], b['email'], b.get('phone'))
        if App.objects.filter(job_id=job['id'], candidate_id=cand).exists():
            raise ConflictError('this candidate has already applied for this job')
        score = score_application(job, b.get('experienceYears'), answers)
        app = App.objects.create(
            company_id=company_id, job_id=job['id'], candidate_id=cand, stage=job['stages'][0], source=b['source'],
            cover_note=b.get('coverNote'), answers=answers, experience_years=_decimal(b.get('experienceYears')),
            current_employer=b.get('currentEmployer'), score=score['score'], score_detail=score['detail'],
            knocked_out=score['knockedOut'], owner_user_id=user.id)
        app_id = str(app.id)
        if b.get('cvFileId'):
            attach_file(company_id=company_id, file_id=b['cvFileId'], user_id=user.id, purpose='hiring_cv',
                        attached_to=f'hiring_application:{app_id}')
            App.objects.filter(pk=app_id).update(cv_file_id=b['cvFileId'])
        add_event(company_id=company_id, application_id=app_id, actor_id=user.id, kind='applied',
                  body=f"Added by {user.name} ({b['source'].replace('_', ' ', 1)})")
    return app_id


def _panel_names(interviews: list[dict]) -> None:
    """Each interview's panel as names in name order (None when nobody on it still exists)."""
    ids = {p for i in interviews for p in i['panel']}
    names = {str(k): v for k, v in AppUser.objects.filter(pk__in=ids).values_list('id', 'name')} if ids else {}
    for i in interviews:
        got = sorted((names[p] for p in i['panel'] if p in names), key=collate_key)
        i['panel_names'] = got or None


def application_detail(user: AuthUser, app_id: str) -> dict:
    app, job, access = load_app(user, app_id, allow_panel=True)
    cid = app['company_id']
    full = _values(
        App.objects.filter(pk=app['id']), id='id', job_id='job_id', candidate_id='candidate_id', stage='stage', status='status',
        source='source', cover_note='cover_note', answers='answers', experience_years='experience_years',
        current_employer='current_employer', score='score', score_detail='score_detail', knocked_out='knocked_out',
        rating='rating', tags='tags', owner_user_id='owner_user_id', rejected_reason='rejected_reason', closed_at='closed_at',
        hired_employee_id='hired_employee_id', consent_at='consent_at', stage_changed_at='stage_changed_at',
        created_at='created_at', has_cv=_flag(Q(cv_file__isnull=False)), name='candidate__name', email='candidate__email',
        phone='candidate__phone', purged_at='candidate__purged_at', owner_name='owner_user__name')[0]
    full['id'] = str(full['id'])
    full['tags'] = full['tags'] or []
    full['answers'] = full['answers'] or {}
    full['score_detail'] = full['score_detail'] or []
    is_full = access == 'full'
    others = _values(App.objects.for_company(cid).filter(candidate_id=app['candidate_id']).exclude(pk=app['id'])
                     .order_by('-created_at'), id='id', stage='stage', status='status', created_at='created_at',
                     title='job__title') if is_full else []
    events = _values(HiringEvent.objects.for_company(cid).filter(application_id=app['id']).order_by('-created_at')[:300],
                     id='id', kind='kind', body='body', data='data', created_at='created_at', actor='actor__name') if is_full else []
    interviews = _values(Iv.objects.for_company(cid).filter(application_id=app['id']).order_by('starts_at'), id='id',
                         title='title', starts_at='starts_at', duration_min='duration_min', mode='mode', location='location',
                         panel='panel', status='status')
    for i in interviews:
        i['id'] = str(i['id'])
        i['panel'] = [str(p) for p in (i['panel'] or [])]
    _panel_names(interviews)
    cards = _values(HiringScorecard.objects.filter(interview_id__in=[i['id'] for i in interviews]).order_by('submitted_at'),
                    id='id', interview_id='interview_id', reviewer_id='reviewer_id', ratings='ratings',
                    recommendation='recommendation', notes='notes', submitted_at='submitted_at', reviewer='reviewer__name')
    for c in cards:
        c['interview_id'], c['reviewer_id'] = str(c['interview_id']), str(c['reviewer_id'])
    scorecards = cards if is_full else [c for c in cards if c['reviewer_id'] == user.id]
    offers = _values(Offer.objects.for_company(cid).filter(application_id=app['id']).order_by('-created_at'), id='id',
                     designation='designation', salary='salary', join_date='join_date', expires_on='expires_on', notes='notes',
                     status='status', created_at='created_at', decided_at='decided_at',
                     responded_at='responded_at') if is_full else []
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


def update_application(user: AuthUser, app_id: str, body: dict) -> None:
    _need_staff(user)
    b = _checked(S.UpdateApplicationIn, body)
    app, _, _ = load_app(user, app_id)
    if b.get('ownerUserId') and b['ownerUserId'] not in _company_user_ids(app['company_id']):
        raise BadRequestError('owner must be an active user in your company')
    one_app = App.objects.filter(pk=app['id'])
    with transaction.atomic():
        if 'rating' in b:
            one_app.update(rating=b['rating'])
            add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='rating',
                      body=f"Rated {b['rating']} of 5" if b['rating'] else 'Rating cleared')
        if 'tags' in b:
            one_app.update(tags=list(dict.fromkeys(b['tags'])))
        if 'ownerUserId' in b:
            one_app.update(owner_user_id=b['ownerUserId'])


def move_application(user: AuthUser, app_id: str, body: dict) -> None:
    _need_staff(user)
    stage = _checked(S.MoveIn, body)['stage']
    with transaction.atomic():
        app, job, _ = load_app(user, app_id, lock=True)
        if app['status'] != App.Status.ACTIVE:
            raise ConflictError(f"this application is {app['status']}")
        stages = job['stages']
        idx = stages.index(stage) if stage in stages else -1
        if idx < 0:
            raise BadRequestError('that stage is not part of this job')
        if idx == len(stages) - 1:
            raise BadRequestError('use "Hire" to move a candidate to Hired once they accept an offer')
        if stage == app['stage']:
            return
        _set_stage(app, stage, idx, user.id)


def reject_application(user: AuthUser, app_id: str, body: dict) -> bool:
    _need_staff(user)
    b = _checked(S.RejectIn, body)
    with transaction.atomic():
        app, job, _ = load_app(user, app_id, lock=True)
        if app['status'] != App.Status.ACTIVE:
            raise ConflictError(f"this application is already {app['status']}")
        App.objects.filter(pk=app['id']).update(status=App.Status.REJECTED, rejected_reason=b['reason'], closed_at=Now())
        add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='rejected', body=b['reason'])
        open_offers = Offer.objects.filter(application_id=app['id'], status__in=OPEN_OFFER).select_for_update()
        ids = [str(i) for i in open_offers.values_list('id', flat=True)]
        Offer.objects.filter(pk__in=ids).update(status=Offer.Status.WITHDRAWN)
        for offer_id in ids:
            cancel_workflow('job_offer', offer_id)
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
        if app['status'] not in CLOSED_APP:
            raise ConflictError(f"this application is {app['status']}")
        if HiringCandidate.objects.filter(pk=app['candidate_id'], purged_at__isnull=False).exists():
            raise ConflictError("this candidate's data has been removed")
        App.objects.filter(pk=app['id']).update(status=App.Status.ACTIVE, rejected_reason=None, closed_at=None)
        add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='reopened', body='Application reopened')


def add_note(user: AuthUser, app_id: str, body: dict) -> None:
    _need_staff(user)
    text = _checked(S.NoteIn, body)['body']
    app, _, _ = load_app(user, app_id)
    add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='note', body=text)


def export_candidate(user: AuthUser, app_id: str) -> dict:
    """Everything held about a candidate, for a data access request. HR only, audited."""
    _need_module(user)
    app, _, _ = load_app(user, app_id)
    cid = app['company_id']
    cand = _values(HiringCandidate.objects.filter(pk=app['candidate_id']), id='id', name='name', email='email', phone='phone',
                   created_at='created_at', purged_at='purged_at')[0]
    apps = _values(App.objects.for_company(cid).filter(candidate_id=app['candidate_id']).order_by('created_at'), id='id',
                   job='job__title', stage='stage', status='status', source='source', cover_note='cover_note', answers='answers',
                   experience_years='experience_years', current_employer='current_employer', score='score',
                   score_detail='score_detail', rating='rating', tags='tags', rejected_reason='rejected_reason',
                   created_at='created_at', closed_at='closed_at', consent_at='consent_at')
    ids = [a['id'] for a in apps]
    events = _values(HiringEvent.objects.filter(application_id__in=ids).order_by('created_at'), application_id='application_id',
                     kind='kind', body='body', created_at='created_at')
    interviews = _values(Iv.objects.filter(application_id__in=ids), application_id='application_id', title='title',
                         starts_at='starts_at', mode='mode', status='status')
    offers = _values(Offer.objects.filter(application_id__in=ids), application_id='application_id', designation='designation',
                     salary='salary', join_date='join_date', status='status', created_at='created_at')
    audit('export', 'hiring_candidate', actor_id=user.id, row_id=app['candidate_id'])
    return {'exportedAt': datetime.now(timezone.utc), 'candidate': cand, 'applications': apps, 'events': events,
            'interviews': interviews, 'offers': offers, '_candidate_id': app['candidate_id']}


# ---------------------------------------------------------------- interviews

def _check_panel(company_id: str, panel: list[str]) -> list[str]:
    """Panel members must be people of the company (members of the group with access count)."""
    ids = list(dict.fromkeys(panel))
    if not set(ids) <= _company_user_ids(company_id):
        raise BadRequestError('every panel member must be an active user in your company')
    return ids


def schedule_interview(user: AuthUser, app_id: str, body: dict) -> str:
    _need_staff(user)
    b = _checked(S.InterviewIn, body)
    starts_at = npt_instant(b['date'], b['time'])
    if starts_at < datetime.now(timezone.utc) - timedelta(hours=1):
        raise BadRequestError('the interview time is in the past')
    with transaction.atomic():
        app, job, _ = load_app(user, app_id, lock=True)
        if app['status'] != App.Status.ACTIVE:
            raise ConflictError(f"this application is {app['status']}")
        panel = _check_panel(app['company_id'], b['panel'])
        iv = Iv.objects.create(company_id=app['company_id'], application_id=app['id'], title=b['title'], starts_at=starts_at,
                               duration_min=b['durationMin'], mode=b['mode'], location=b.get('location'), panel=panel,
                               created_by_id=user.id)
        interview_id = str(iv.id)
        starts_iso = _iso(starts_at)
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
            if b['mode'] == Iv.Mode.VIDEO:
                how = f"by video{f' ({loc})' if loc else ''}"
            elif b['mode'] == Iv.Mode.PHONE:
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


def list_interviews(user: AuthUser, query: dict) -> list[dict]:
    company_id = company_id_of(user)
    q = _checked(S.InterviewsQueryIn, {k: v for k, v in (query or {}).items() if v not in (None, '')})
    if q['scope'] == 'all' and not has_role(user, 'manager', 'hr', 'recruiter'):
        raise ForbiddenError('only managers and HR can see all interviews')
    qs = Iv.objects.for_company(company_id)
    if q.get('from'):
        qs = qs.filter(starts_at__gte=_npt_day_start(q['from']))
    if q.get('to'):
        qs = qs.filter(starts_at__lt=_npt_day_start(q['to']) + timedelta(days=1))
    mine = Q(panel__contains=[user.id])
    if q['scope'] == 'mine':
        qs = qs.filter(mine)
    else:
        scope = _in_scope(user, created_by='application__job__created_by_id', hiring_manager='application__job__hiring_manager_id',
                          org_unit='application__job__org_unit_id')
        if scope is not None:
            qs = qs.filter(mine | scope)
    cards = HiringScorecard.objects.filter(interview=OuterRef('pk'))
    out = _values(qs.order_by('starts_at')[:500], id='id', application_id='application_id', title='title', starts_at='starts_at',
                  duration_min='duration_min', mode='mode', location='location', status='status', panel='panel',
                  candidate='application__candidate__name', job='application__job__title', job_id='application__job_id',
                  on_panel=_flag(mine), my_scorecard=Exists(cards.filter(reviewer_id=user.id)), scorecards=count_of(cards))
    for i in out:
        i['id'], i['application_id'] = str(i['id']), str(i['application_id'])
        i['panel'] = [str(p) for p in (i['panel'] or [])]
    _panel_names(out)
    return out


def update_interview(user: AuthUser, interview_id: str, body: dict) -> None:
    _need_staff(user)
    b = _checked(S.InterviewPatchIn, body)
    interview_id = parse_uuid(interview_id)
    with transaction.atomic():
        i = Iv.objects.for_company(company_id_of(user)).select_for_update().filter(pk=interview_id).first()
        if not i:
            raise NotFoundError('interview not found')
        app, _, _ = load_app(user, str(i.application_id))
        changes: dict[str, Any] = {}
        if b.get('status'):
            changes['status'] = b['status']
        if b.get('date') or b.get('time'):
            cur = i.starts_at.astimezone(NPT)
            changes['starts_at'] = npt_instant(b.get('date') or cur.date().isoformat(), b.get('time') or cur.strftime('%H:%M'))
        if 'location' in b:
            changes['location'] = b['location']
        if b.get('panel'):
            changes['panel'] = _check_panel(app['company_id'], b['panel'])
        if not changes:
            return
        Iv.objects.filter(pk=interview_id).update(**changes)
        add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='interview',
                  data={'interviewId': interview_id},
                  body='Interview cancelled' if b.get('status') == Iv.Status.CANCELLED else 'Interview completed'
                  if b.get('status') == Iv.Status.COMPLETED else 'Interview updated')


def _my_interview(user: AuthUser, interview_id: str):
    """An interview of the active company this person sits on, else 'interview not found'."""
    interview_id = parse_uuid(interview_id)
    i = (Iv.objects.for_company(company_id_of(user)).filter(pk=interview_id, panel__contains=[user.id])
         .select_related('application__candidate').first())
    if not i:
        raise NotFoundError('interview not found')
    return i


def submit_scorecard(user: AuthUser, interview_id: str, body: dict) -> None:
    """Panel members score the interview they sat on; one scorecard each, editable until the candidate is hired."""
    b = _checked(S.ScorecardIn, body)
    with transaction.atomic():
        i = _my_interview(user, interview_id)
        if i.status == Iv.Status.CANCELLED:
            raise ConflictError('this interview was cancelled')
        if i.application.status == App.Status.HIRED:
            raise ConflictError('this candidate has already been hired')
        HiringScorecard.objects.bulk_create(
            [HiringScorecard(interview_id=i.id, reviewer_id=user.id, ratings=b['ratings'], recommendation=b['recommendation'],
                             notes=b.get('notes'), submitted_at=datetime.now(timezone.utc))],
            update_conflicts=True, unique_fields=['interview', 'reviewer'],
            update_fields=['ratings', 'recommendation', 'notes', 'submitted_at'])
        add_event(company_id=str(i.company_id), application_id=str(i.application_id), actor_id=user.id, kind='scorecard',
                  body=f"Scorecard: {b['recommendation'].replace('_', ' ', 1)}", data={'interviewId': str(i.id)})


def interview_for_scorecard(user: AuthUser, interview_id: str) -> dict:
    """The interview a panel member is about to score, with their saved scorecard if any."""
    i = _my_interview(user, interview_id)
    mine = (HiringScorecard.objects.filter(interview_id=i.id, reviewer_id=user.id)
            .values('ratings', 'recommendation', 'notes').first())
    return {'id': str(i.id), 'title': i.title, 'panel': list(i.panel), 'status': i.status, 'application_id': i.application_id,
            'candidate': i.application.candidate.name, 'mine': mine}


# ---------------------------------------------------------------- offers

def create_offer(user: AuthUser, app_id: str, body: dict) -> str:
    _need_module(user)
    b = _checked(S.OfferIn, body)
    if b['joinDate'] < npt_today().isoformat():
        raise BadRequestError('the joining date is in the past')
    if b.get('expiresOn') and b['expiresOn'] > b['joinDate']:
        raise BadRequestError('the offer should expire on or before the joining date')
    with transaction.atomic():
        app, job, _ = load_app(user, app_id, lock=True)
        if app['status'] != App.Status.ACTIVE:
            raise ConflictError(f"this application is {app['status']}")
        if Offer.objects.filter(application_id=app['id'], status__in=OPEN_OFFER).exists():
            raise ConflictError('this candidate already has an open offer; withdraw it first')
        offer = Offer.objects.create(
            company_id=app['company_id'], application_id=app['id'], designation=b['designation'], salary=b['salary'],
            join_date=b['joinDate'], expires_on=b.get('expiresOn'), notes=b.get('notes'), created_by_id=user.id,
            created_by_employee_id=user.employee_id)
        offer_id = str(offer.id)
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
    qs = Offer.objects.for_company(company_id_of(user)).filter(pk=offer_id)
    if lock:
        qs = qs.select_for_update()
    found = _values(qs, **_columns(Offer))
    if not found:
        raise NotFoundError('offer not found')
    o = found[0]
    o['id'] = str(o['id'])
    app, job, _ = load_app(user, str(o['application_id']))
    return o, app, job


def offer_letter(user: AuthUser, offer_id: str) -> str:
    _need_staff(user)
    o, app, job = _load_offer(user, offer_id)
    extra = _values(Job.objects.filter(pk=job['id']), department='org_unit__name_en', location='location',
                    employment_type='employment_type', code='company__code', name_en='company__name_en')[0]
    return render_offer_letter(
        company=extra['name_en'], candidate=app['name'], designation=o['designation'], department=extra['department'],
        location=extra['location'], employment_type=extra['employment_type'], salary=o['salary'], join_date=o['join_date'],
        expires_on=o['expires_on'], notes=o['notes'], issued_on=o['created_at'].astimezone(NPT).date().isoformat(),
        reference=f"{extra['code']}/HR/OFFER/{o['id'][-6:].upper()}",
        draft=o['status'] not in (Offer.Status.APPROVED, Offer.Status.ACCEPTED))


def respond_offer(user: AuthUser, offer_id: str, body: dict) -> None:
    _need_module(user)
    response = _checked(S.RespondIn, body)['response']
    with transaction.atomic():
        o, app, _ = _load_offer(user, offer_id, lock=True)
        if o['status'] != Offer.Status.APPROVED:
            raise ConflictError('the offer must be approved before the candidate can respond'
                                if o['status'] == Offer.Status.PENDING_APPROVAL
                                else f"this offer is {o['status'].replace('_', ' ', 1)}")
        Offer.objects.filter(pk=o['id']).update(status=response, responded_at=Now())
        add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='offer',
                  body=f'Offer {response} by the candidate', data={'offerId': o['id']})
        audit(response, 'hiring_offer', actor_id=user.id, row_id=o['id'])


def withdraw_offer(user: AuthUser, offer_id: str) -> None:
    _need_module(user)
    with transaction.atomic():
        o, app, _ = _load_offer(user, offer_id, lock=True)
        if o['status'] not in OPEN_OFFER:
            raise ConflictError(f"this offer is {o['status'].replace('_', ' ', 1)}")
        if app['status'] == App.Status.HIRED:
            raise ConflictError('the candidate has already been hired')
        Offer.objects.filter(pk=o['id']).update(status=Offer.Status.WITHDRAWN)
        cancel_workflow('job_offer', o['id'])
        add_event(company_id=app['company_id'], application_id=app['id'], actor_id=user.id, kind='offer', body='Offer withdrawn',
                  data={'offerId': o['id']})
        audit('withdraw', 'hiring_offer', actor_id=user.id, row_id=o['id'])


# ---------------------------------------------------------------- hire

def hire(user: AuthUser, app_id: str, body: dict) -> str:
    """
    Turn a candidate who accepted an offer into an employee of the active company; the CV stays linked through the
    application. The employee row, its assignment and shift are written here exactly as the TS route does (no people
    service is involved); the career-timeline and onboarding steps run through hajiri.integrations.hooks.run_hire_hooks.
    """
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')
    b = _checked(S.HireIn, body)
    company_id = company_id_of(user)
    _check_refs(company_id, [('branch', b.get('branchId'), 'branch'), ('org_unit', b.get('orgUnitId'), 'department'),
                             ('employee', b.get('managerId'), 'manager'), ('shift', b.get('shiftId'), 'shift')])
    with transaction.atomic():
        app, job, _ = load_app(user, app_id, lock=True)
        if app['status'] != App.Status.ACTIVE:
            raise ConflictError(f"this application is {app['status']}")
        offer = (Offer.objects.filter(application_id=app['id'], status=Offer.Status.ACCEPTED).order_by('-created_at')
                 .values('designation', 'join_date').first())
        if not offer:
            raise ConflictError('the candidate must accept an approved offer before they can be hired')
        if Employee.objects.for_company(company_id).filter(emp_code=b['empCode']).exists():
            raise ConflictError('this employee code is already in use')
        join_date = b.get('joinDate') or offer['join_date'].isoformat()
        if b.get('probationEnd') and b['probationEnd'] < join_date:
            raise BadRequestError('probation end cannot be before join date')
        emp = Employee.objects.create(
            company_id=company_id, emp_code=b['empCode'], name_en=app['name'], gender=b.get('gender'), join_date=join_date,
            designation=b.get('designation') or offer['designation'], phone=app['phone'], email=app['email'],
            probation_end=b.get('probationEnd'), weekly_off_days=[6], regime=Employee.Regime.SSF)
        emp_id = str(emp.id)
        EmployeeAssignment.objects.create(
            employee_id=emp_id, valid=DateRange(date.fromisoformat(join_date), None), branch_id=b.get('branchId') or job['branch_id'],
            org_unit_id=b['orgUnitId'] if 'orgUnitId' in b else job['org_unit_id'],
            manager_id=b['managerId'] if 'managerId' in b else job['hiring_manager_id'], employment_type=b['employmentType'])
        if b.get('shiftId'):
            ShiftAssignment.objects.create(employee_id=emp_id, shift_id=b['shiftId'], valid=DateRange(date.fromisoformat(join_date), None))
        stages = job['stages']
        App.objects.filter(pk=app['id']).update(status=App.Status.HIRED, stage=stages[-1], furthest_stage=len(stages) - 1,
                                                hired_employee_id=emp_id, closed_at=Now(), stage_changed_at=Now())
        if job['requisition_id']:
            # F() reads the row as it was before this UPDATE, like the SQL CASE in SET.
            Req.objects.filter(pk=job['requisition_id']).update(
                filled=F('filled') + 1,
                status=Case(When(filled__gte=F('headcount') - 1, then=Value(Req.Status.FILLED)), default=F('status'),
                            output_field=TextField()))
        add_event(company_id=company_id, application_id=app['id'], actor_id=user.id, kind='hired', body=f"Hired as {b['empCode']}",
                  data={'employeeId': emp_id})
        audit('hire', 'employee', actor_id=user.id, row_id=emp_id,
              after={'applicationId': app['id'], 'empCode': b['empCode'], 'joinDate': join_date})
        run_hire_hooks(HireEvent(company_id=company_id, employee_id=emp_id, application_id=app['id'], job_id=job['id'],
                                 join_date=join_date, actor_id=user.id))
    return emp_id


# ---------------------------------------------------------------- lookups, reports

def lookups(user: AuthUser) -> dict:
    """Names for the hiring forms: people who can sit on a panel or own a candidate, departments, branches and shifts."""
    _need_staff(user)
    company_id = company_id_of(user)
    return {
        'users': [{'id': u['id'], 'name': u['name'], 'roles': u['roles']} for u in company_users(company_id)],
        'orgUnits': list(OrgUnit.objects.for_company(company_id).order_by('name_en').values('id', 'name_en', 'kind')),
        'branches': list(Branch.objects.for_company(company_id).order_by('name_en').values('id', 'name_en')),
        'shifts': list(Shift.objects.for_company(company_id).order_by('name_en').values('id', 'code', 'name_en')),
    }


def _one_decimal(v) -> int | float | None:
    if v is None:
        return None
    f = float(Decimal(str(v)).quantize(Decimal('0.1'), ROUND_HALF_UP))
    return int(f) if f.is_integer() else f


def reports(user: AuthUser, query: dict | None = None) -> dict:
    _need_module(user)
    company_id = company_id_of(user)
    q = _checked(S.ReportsQueryIn, {k: v for k, v in (query or {}).items() if v not in (None, '')})
    window = App.objects.for_company(company_id)
    if q.get('from'):
        window = window.filter(created_at__gte=_npt_day_start(q['from']))
    if q.get('to'):
        window = window.filter(created_at__lt=_npt_day_start(q['to']) + timedelta(days=1))
    jobs = (Job.objects.for_company(company_id).filter(Exists(window.filter(job=OuterRef('pk')))).order_by('-created_at')
            .values('id', 'title', 'status', 'stages'))
    reached = list(window.values('job_id', 'furthest_stage', 'status'))
    funnel = []
    for j in jobs:
        apps = [x for x in reached if x['job_id'] == j['id']]
        funnel.append({'jobId': str(j['id']), 'title': j['title'], 'status': j['status'], 'total': len(apps),
                       'rejected': sum(1 for a in apps if a['status'] == App.Status.REJECTED),
                       'stages': [{'stage': s, 'reached': sum(1 for a in apps if a['furthest_stage'] >= i)}
                                  for i, s in enumerate(j['stages'] or [])]})
    days = ExpressionWrapper(Extract(F('closed_at') - F('created_at'), 'epoch') / 86400, output_field=DecimalField())
    tth = window.filter(status=App.Status.HIRED).aggregate(avg=Avg(days, output_field=DecimalField()), median=Median(days),
                                                          hires=Count('id'))
    sources = list(window.order_by().values('source')
                   .annotate(applications=Count('id'), hired=Count('id', filter=Q(status=App.Status.HIRED)))
                   .order_by('-applications').values('source', 'applications', 'hired'))
    totals = window.aggregate(applications=Count('id'), active=Count('id', filter=Q(status=App.Status.ACTIVE)),
                              hired=Count('id', filter=Q(status=App.Status.HIRED)),
                              rejected=Count('id', filter=Q(status=App.Status.REJECTED)))
    return {'totals': totals, 'funnel': funnel, 'sources': sources,
            'timeToHire': {'averageDays': _one_decimal(tth['avg']), 'medianDays': _one_decimal(tth['median']),
                           'hires': tth['hires'] or 0}}


def policy(user: AuthUser) -> dict:
    _need_staff(user)
    return hiring_policy(company_id_of(user))


def careers_url(request_base: str, job_id: str | None = None, code: str | None = None) -> str:
    """The public careers page of a company (/careers/<code>) or of one of its jobs (/careers/<code>/<job id>): the
    company code in the path keeps the link right when several companies share one Hajiri."""
    from django.urls import reverse
    if code:
        path = reverse('careers:company_job', args=[code, job_id]) if job_id else reverse('careers:detail', args=[code])
    else:
        path = reverse('careers:detail', args=[job_id]) if job_id else reverse('careers:list')
    return f'{request_base}{path}'
