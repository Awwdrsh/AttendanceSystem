"""
Public careers site (port of apps/api/src/modules/careers/routes.ts): open jobs and job applications, no session.

Each company has its own careers page, named by its code in the address (/careers/<code>, or ?company=CODE as the
TS app linked it); an install with a single company needs no code. Several companies of a group on one Hajiri each
show only their own jobs, and under a company's address only that company's jobs are found. Nothing but open jobs'
public fields ever leaves this module.
"""
from __future__ import annotations

import threading
import time
import uuid

import django_filters
from django.db import transaction
from django.db.models import Case, F, Q, When
from django.db.models.functions import Now

from hajiri.core.clock import npt_today
from hajiri.core.errors import NotFoundError, TooManyRequestsError
from hajiri.core.scoping import is_uuid
from hajiri.core.validation import plain, validate
from hajiri.db.models import Company, HiringApplication, HiringJob
from hajiri.features.service import feature_on
from hajiri.files.store import ParsedFile, check_file, parse_upload, save_file
from hajiri.hiring.serializers import ApplyFormIn, ApplyIn, CareersQueryIn
from hajiri.hiring.services import (add_event, clean_answers, company_name, email_candidate, hiring_policy, job_watchers,
                                    score_application, upsert_candidate)
from hajiri.notifications.service import notify

OK_MESSAGE = 'Thank you. Your application has been received.'
GONE = 'This job is no longer accepting applications.'
PAGE_GONE = 'careers page not found'


class RateLimiter:
    """In-memory sliding window per client address: enough to blunt scripted floods on a single-server install."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window_s = limit, window_s
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            recent = [t for t in self._hits.get(key, []) if now - t < self.window_s]
            if len(recent) >= self.limit:
                self._hits[key] = recent
                return False
            recent.append(now)
            self._hits[key] = recent
            if len(self._hits) > 10_000:
                for k in [k for k, v in self._hits.items() if not any(now - t < self.window_s for t in v)]:
                    del self._hits[k]
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


apply_limiter = RateLimiter(5, 60 * 60)


def _public_salary(field: str) -> Case:
    return Case(When(show_salary=True, then=F(field)), default=None)


# The public fields of a job, keyed as the TS API answered them.
PUBLIC_JOB = {
    'id': 'id', 'title': 'title', 'description': 'description', 'requirements': 'requirements', 'category': 'category',
    'location': 'location', 'work_mode': 'work_mode', 'employment_type': 'employment_type', 'experience_min': 'experience_min',
    'experience_max': 'experience_max', 'salary_min': _public_salary('salary_min'), 'salary_max': _public_salary('salary_max'),
    'openings': 'openings', 'deadline': 'deadline', 'published_at': 'published_at', 'department': 'org_unit__name_en',
}


def public_questions(qs: list[dict]) -> list[dict]:
    """Only questions' public parts go out; knock-out and preferred answers stay private."""
    return [{'id': q['id'], 'kind': q['kind'], 'label': q['label'], 'required': q.get('required', True)} for q in qs]


def _careers_on(company_id: str) -> bool:
    return hiring_policy(company_id)['careersEnabled'] and feature_on(company_id, 'hiring')


def _public_values(qs) -> list[dict]:
    ann = {f'v_{k}': (F(v) if isinstance(v, str) else v) for k, v in PUBLIC_JOB.items()}
    out = [{k: r[f'v_{k}'] for k in PUBLIC_JOB} for r in qs.annotate(**ann).values(*ann)]
    for j in out:
        j['id'] = str(j['id'])
    return out


def _companies_by_code(code: str):
    return Company.objects.filter(code__iexact=code)


def resolve_company(code: str | None) -> dict:
    """The company whose careers page this is: by its code (any case), or the only company of the install."""
    qs = _companies_by_code(code) if code else Company.objects.order_by('created_at')
    found = list(qs.values('id', 'code', 'name_en')[:2])
    if len(found) != 1:
        raise NotFoundError(PAGE_GONE)
    c = {**found[0], 'id': str(found[0]['id'])}
    if not _careers_on(c['id']):
        raise NotFoundError(PAGE_GONE)
    return c


def open_jobs(company_id: str | None):
    """A company's jobs that take applications today (open, deadline not passed in Nepal). Without a company, those
    of every company: only for the TS address /careers/<job id>, where the job itself names its company."""
    qs = HiringJob.objects.for_company(company_id) if company_id else HiringJob.objects.all()
    return qs.filter(status=HiringJob.Status.OPEN).filter(Q(deadline__isnull=True) | Q(deadline__gte=npt_today()))


class OpenJobFilter(django_filters.FilterSet):
    """The careers page's filters: words in the title or description, work mode, category and experience."""
    q = django_filters.CharFilter(method='search')
    workMode = django_filters.CharFilter(field_name='work_mode')
    category = django_filters.CharFilter(field_name='category')
    experience = django_filters.NumberFilter(method='fits_experience')

    class Meta:
        model = HiringJob
        fields: list[str] = []

    def search(self, qs, name, value):
        # icontains escapes % and _, so they match themselves.
        return qs.filter(Q(title__icontains=value) | Q(description__icontains=value))

    def fits_experience(self, qs, name, value):
        return qs.filter(Q(experience_min__isnull=True) | Q(experience_min__lte=value))


def _company_id_for(code: str | None) -> str | None:
    """The company a careers address names (None when it names none). An unknown code finds no job."""
    if not code:
        return None
    found = list(_companies_by_code(code).values_list('id', flat=True)[:2])
    if len(found) != 1:
        raise NotFoundError(GONE)
    return str(found[0])


def open_job(job_id: str, company_id: str | None = None) -> dict:
    """An open job by id (of this company, when given), with what scoring an application needs."""
    if not is_uuid(job_id):
        raise NotFoundError(GONE)
    j = (open_jobs(company_id).filter(pk=job_id.lower())
         .values('id', 'company_id', 'title', 'questions', 'stages', 'experience_min', 'experience_max', 'hiring_manager_id').first())
    if not j or not _careers_on(str(j['company_id'])):
        raise NotFoundError(GONE)
    j['id'], j['company_id'] = str(j['id']), str(j['company_id'])
    j['questions'] = j['questions'] or []
    j['stages'] = j['stages'] or []
    return j


def _clean_query(query: dict) -> dict:
    known = CareersQueryIn().fields
    return {k: v for k, v in (query or {}).items() if k in known and v is not None}


def list_jobs(query: dict, company_code: str | None = None) -> dict:
    """The open jobs of one company's careers page. `company_code` (from the address) wins over ?company=."""
    q = plain(validate(CareersQueryIn, _clean_query(query)))
    company = resolve_company(company_code or q.get('company'))
    filters = {k: q[k] for k in ('q', 'workMode', 'category', 'experience') if q.get(k) not in (None, '')}
    qs = open_jobs(company['id'])
    jobs = OpenJobFilter(filters, queryset=qs).qs.order_by(F('published_at').desc(nulls_last=True))[:200]
    cats = qs.exclude(category=None).order_by('category').values_list('category', flat=True).distinct()
    return {'company': {'name': company['name_en'], 'code': company['code']}, 'jobs': _public_values(jobs), 'categories': list(cats)}


def get_job(job_id: str, company_code: str | None = None) -> dict:
    """One open job with its public questions; with a company code, only that company's job is found."""
    job = open_job(job_id, _company_id_for(company_code))
    row = _public_values(HiringJob.objects.for_company(job['company_id']).filter(pk=job['id']))[0]
    co = Company.objects.for_company(job['company_id']).values('name_en', 'code').get()
    return {'company': {'name': co['name_en'], 'code': co['code']}, 'job': {**row, 'questions': public_questions(job['questions'])}}


def apply(job_id: str, body: dict, ip: str | None, company_code: str | None = None, *,
          cv_upload: ParsedFile | None = None) -> dict:
    """Store an application (or pretend to, for a bot or a repeat). The answer is always the same 201 message.
    JSON clients send the CV as `cvDataUrl`; the careers page passes the uploaded file as `cv_upload`."""
    ok = {'ok': True, 'message': OK_MESSAGE}
    if not apply_limiter.allow(ip or 'unknown'):
        raise TooManyRequestsError('Too many applications from this connection. Please try again later.')
    if not is_uuid(job_id):
        raise NotFoundError(GONE)
    b = plain(validate(ApplyIn if cv_upload is None else ApplyFormIn, body))
    if b.get('website') and b['website'].strip() != '':
        return ok   # a bot filled the hidden field: pretend, store nothing
    job = open_job(job_id, _company_id_for(company_code))
    answers = clean_answers(job['questions'], b.get('answers'))
    cv = (parse_upload(b['cvDataUrl'], allowed=('application/pdf',)) if cv_upload is None
          else check_file(cv_upload.mime, cv_upload.data, allowed=('application/pdf',)))
    co = job['company_id']
    with transaction.atomic():
        cand = upsert_candidate(co, b['name'], b['email'], b['phone'])
        if HiringApplication.objects.for_company(co).filter(job_id=job['id'], candidate_id=cand).exists():
            # Same answer either way, so the form never reveals whether an address has applied before.
            transaction.set_rollback(True)
            return ok
        app_id = str(uuid.uuid4())   # the CV is stored attached to the application, so the id comes first
        f = save_file(company_id=co, purpose='hiring_cv', filename=b['cvFilename'], file=cv, uploaded_by=None,
                      attached_to=f'hiring_application:{app_id}')
        score = score_application(job, b.get('experienceYears'), answers)
        HiringApplication.objects.create(
            id=app_id, company_id=co, job_id=job['id'], candidate_id=cand, stage=job['stages'][0], source=b['source'],
            cv_file_id=f['id'], cover_note=b.get('coverNote'), answers=answers,
            experience_years=None if b.get('experienceYears') is None else str(b['experienceYears']),
            current_employer=b.get('currentEmployer'), score=score['score'], score_detail=score['detail'],
            knocked_out=score['knockedOut'], consent_at=Now())
        add_event(company_id=co, application_id=app_id, actor_id=None, kind='applied',
                  body=f"Applied through the careers page ({b['source'].replace('_', ' ', 1)})")
        company = company_name(co)
        email_candidate(
            company_id=co, application_id=app_id, to=b['email'].lower(), actor_id=None, template='hiring.received',
            title=f"We received your application for {job['title']}",
            body=(f"Dear {b['name']},\n\nThank you for applying for the {job['title']} role at {company}. Our team will review your "
                  f"application and contact you if your profile matches what we are looking for.\n\n{company}"))
        for user_id in job_watchers(co, job['hiring_manager_id'] and str(job['hiring_manager_id'])):
            notify(company_id=co, user_id=user_id, template='hiring.applied', title=f"New application: {job['title']}",
                   body=f"{b['name']} applied for {job['title']}.", payload={'applicationId': app_id, 'jobId': job['id']})
    return ok
