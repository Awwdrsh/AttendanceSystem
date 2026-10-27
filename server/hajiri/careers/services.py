"""
Public careers site (port of apps/api/src/modules/careers/routes.ts): open jobs and job applications, no session. A
company is chosen by its code (?company=CODE); an install with a single company needs no code. Nothing but open jobs'
public fields ever leaves this module.
"""
from __future__ import annotations

import json
import threading
import time
import uuid

from django.db import transaction

from hajiri.core.db import one, rows
from hajiri.core.errors import NotFoundError, TooManyRequestsError
from hajiri.core.schema import Bool, Enum, Num, Obj, Record, S, Str, Union, _Issue, _Issues, is_uuid, parse
from hajiri.features.service import feature_on
from hajiri.files.store import parse_upload, save_file
from hajiri.hiring.services import (add_event, clean_answers, email_candidate, hiring_policy, job_watchers, score_application,
                                    upsert_candidate)
from hajiri.notifications.service import notify
from hajiri.workflow.service import npt_today

OK_MESSAGE = 'Thank you. Your application has been received.'
GONE = 'This job is no longer accepting applications.'


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

PUBLIC_JOB_COLS = """j.id, j.title, j.description, j.requirements, j.category, j.location, j.work_mode, j.employment_type, j.experience_min,
  j.experience_max, CASE WHEN j.show_salary THEN j.salary_min END AS salary_min, CASE WHEN j.show_salary THEN j.salary_max END AS salary_max,
  j.openings, j.deadline, j.published_at, ou.name_en AS department"""

OPEN = "j.status = 'open' AND (j.deadline IS NULL OR j.deadline >= %(today)s::date)"

class _Answers(Record):
    """z.record(z.string().max(20), ...): a key longer than 20 characters is an 'Invalid key in record' issue."""

    def check(self, v, path):
        out = super().check(v, path)
        bad = [k for k in out if len(k) > 20]
        if bad:
            raise _Issues([_Issue(path + [k], 'Invalid key in record') for k in bad])
        return out


APPLY_BODY = Obj({
    'name': Str(trim=True, min=2, max=120),
    'email': Str(email=True, max=200),
    'phone': Str(trim=True, regex=r'^[0-9+\- ()]{6,30}$', regex_message='enter a valid phone number'),
    'experienceYears': Num(min=0, max=60, nullish=True),
    'currentEmployer': Str(trim=True, max=120, nullish=True),
    'coverNote': Str(trim=True, max=4000, nullish=True),
    'answers': _Answers(Union(Str(max=1000), Num(), Bool()), optional=True),
    'cvFilename': Str(trim=True, min=1, max=200),
    'cvDataUrl': Str(min=20, max=6_000_000),
    'consent': S(refine=(lambda v: v is True, 'please agree to the processing of your data')),
    'source': Enum(('careers', 'job_board', 'referral', 'other'), default='careers'),
    'website': Str(max=200, optional=True),   # honeypot: hidden from people, filled in by bots
}, strict=True)

LIST_QUERY = Obj({
    'company': Str(max=32, optional=True), 'q': Str(trim=True, max=100, optional=True),
    'workMode': Enum(('onsite', 'remote', 'hybrid'), optional=True), 'category': Str(max=60, optional=True),
    'experience': Num(coerce=True, min=0, max=60, optional=True),
})


def public_questions(qs: list[dict]) -> list[dict]:
    """Only questions' public parts go out; knock-out and preferred answers stay private."""
    return [{'id': q['id'], 'kind': q['kind'], 'label': q['label'], 'required': q.get('required', True)} for q in qs]


def _careers_on(company_id: str) -> bool:
    return hiring_policy(company_id)['careersEnabled'] and feature_on(company_id, 'hiring')


def resolve_company(code: str | None) -> dict:
    found = (rows('SELECT id, code, name_en FROM company WHERE lower(code) = lower(%s)', [code]) if code
             else rows('SELECT id, code, name_en FROM company ORDER BY created_at LIMIT 2'))
    if len(found) != 1:
        raise NotFoundError('careers page not found')
    c = found[0]
    c['id'] = str(c['id'])
    if not _careers_on(c['id']):
        raise NotFoundError('careers page not found')
    return c


def open_job(job_id: str) -> dict:
    j = one(f"""SELECT j.id, j.company_id, j.title, j.questions, j.stages, j.experience_min, j.experience_max, j.hiring_manager_id
                  FROM hiring_job j WHERE j.id = %(id)s AND {OPEN}""", {'id': job_id, 'today': npt_today()})
    if not j or not _careers_on(str(j['company_id'])):
        raise NotFoundError(GONE)
    j['id'], j['company_id'] = str(j['id']), str(j['company_id'])
    j['questions'] = j['questions'] or []
    j['stages'] = j['stages'] or []
    return j


def _clean_query(query: dict) -> dict:
    return {k: v for k, v in (query or {}).items() if k in LIST_QUERY.fields and v not in (None,)}


def list_jobs(query: dict) -> dict:
    q = parse(LIST_QUERY, _clean_query(query))
    company = resolve_company(q.get('company'))
    term = q.get('q')
    if term:
        term = ''.join(f'\\{c}' if c in '\\%_' else c for c in term)
    p = {'co': company['id'], 'today': npt_today(), 'mode': q.get('workMode'), 'cat': q.get('category'),
         'exp': q.get('experience'), 'q': term or None}
    jobs = rows(f"""SELECT {PUBLIC_JOB_COLS} FROM hiring_job j LEFT JOIN org_unit ou ON ou.id = j.org_unit_id
                     WHERE j.company_id = %(co)s AND {OPEN}
                       AND (%(mode)s::text IS NULL OR j.work_mode = %(mode)s) AND (%(cat)s::text IS NULL OR j.category = %(cat)s)
                       AND (%(exp)s::numeric IS NULL OR j.experience_min IS NULL OR j.experience_min <= %(exp)s)
                       AND (%(q)s::text IS NULL OR j.title ILIKE '%%' || %(q)s || '%%' OR j.description ILIKE '%%' || %(q)s || '%%')
                     ORDER BY j.published_at DESC NULLS LAST LIMIT 200""", p)
    cats = rows(f"""SELECT DISTINCT j.category FROM hiring_job j WHERE j.company_id = %(co)s AND {OPEN} AND j.category IS NOT NULL
                     ORDER BY 1""", {'co': company['id'], 'today': npt_today()})
    for j in jobs:
        j['id'] = str(j['id'])
    return {'company': {'name': company['name_en'], 'code': company['code']}, 'jobs': jobs, 'categories': [c['category'] for c in cats]}


def get_job(job_id: str) -> dict:
    if not is_uuid(job_id):
        raise NotFoundError(GONE)
    job = open_job(job_id.lower())
    row = one(f"""SELECT {PUBLIC_JOB_COLS}, co.name_en AS company_name, co.code AS company_code FROM hiring_job j
                   LEFT JOIN org_unit ou ON ou.id = j.org_unit_id JOIN company co ON co.id = j.company_id WHERE j.id = %s""", [job['id']])
    name, code = row.pop('company_name'), row.pop('company_code')
    row['id'] = str(row['id'])
    return {'company': {'name': name, 'code': code}, 'job': {**row, 'questions': public_questions(job['questions'])}}


def apply(job_id: str, body: dict, ip: str | None) -> dict:
    """Store an application (or pretend to, for a bot or a repeat). The answer is always the same 201 message."""
    ok = {'ok': True, 'message': OK_MESSAGE}
    if not apply_limiter.allow(ip or 'unknown'):
        raise TooManyRequestsError('Too many applications from this connection. Please try again later.')
    if not is_uuid(job_id):
        raise NotFoundError(GONE)
    b = parse(APPLY_BODY, body)
    if b.get('website') and b['website'].strip() != '':
        return ok   # a bot filled the hidden field: pretend, store nothing
    job = open_job(job_id.lower())
    answers = clean_answers(job['questions'], b.get('answers'))
    cv = parse_upload(b['cvDataUrl'], allowed=('application/pdf',))
    with transaction.atomic():
        cand = upsert_candidate(job['company_id'], b['name'], b['email'], b['phone'])
        if one('SELECT 1 AS x FROM hiring_application WHERE job_id = %s AND candidate_id = %s', [job['id'], cand]):
            # Same answer either way, so the form never reveals whether an address has applied before.
            transaction.set_rollback(True)
            return ok
        app_id = str(uuid.uuid4())
        f = save_file(company_id=job['company_id'], purpose='hiring_cv', filename=b['cvFilename'], file=cv, uploaded_by=None,
                      attached_to=f'hiring_application:{app_id}')
        score = score_application(job, b.get('experienceYears'), answers)
        one("""INSERT INTO hiring_application (id, company_id, job_id, candidate_id, stage, source, cv_file_id, cover_note, answers,
                      experience_years, current_employer, score, score_detail, knocked_out, consent_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) RETURNING id""",
            [app_id, job['company_id'], job['id'], cand, job['stages'][0], b['source'], f['id'], b.get('coverNote'), json.dumps(answers),
             b.get('experienceYears'), b.get('currentEmployer'), score['score'], json.dumps(score['detail']), score['knockedOut']])
        add_event(company_id=job['company_id'], application_id=app_id, actor_id=None, kind='applied',
                  body=f"Applied through the careers page ({b['source'].replace('_', ' ', 1)})")
        company = one('SELECT name_en FROM company WHERE id = %s', [job['company_id']])['name_en']
        email_candidate(
            company_id=job['company_id'], application_id=app_id, to=b['email'].lower(), actor_id=None, template='hiring.received',
            title=f"We received your application for {job['title']}",
            body=(f"Dear {b['name']},\n\nThank you for applying for the {job['title']} role at {company}. Our team will review your "
                  f"application and contact you if your profile matches what we are looking for.\n\n{company}"))
        for user_id in job_watchers(job['company_id'], job['hiring_manager_id'] and str(job['hiring_manager_id'])):
            notify(company_id=job['company_id'], user_id=user_id, template='hiring.applied', title=f"New application: {job['title']}",
                   body=f"{b['name']} applied for {job['title']}.", payload={'applicationId': app_id, 'jobId': job['id']})
    return ok

