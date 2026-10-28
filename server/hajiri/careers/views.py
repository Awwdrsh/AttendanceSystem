"""
Public careers site, one per company: /careers/<code> lists a company's open jobs and /careers/<code>/<job id> shows
one with the application form (CareersPage.tsx). The TS addresses keep working: /careers?company=CODE, /careers for
an install with one company, and /careers/<job id>. No sign-in.

The JSON endpoints under /api/v1/public/careers/ are for company websites and job boards that list or post to a
careers page from elsewhere (the TS routes and shapes, plus /api/v1/public/careers/companies/<code>/jobs... naming
the company). They are DRF views without authentication: anyone may call them, no session is read and no CSRF
token is needed, since nothing they do acts as a signed-in person.
"""
from __future__ import annotations

from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.errors import HttpError, NotFoundError
from hajiri.core.scoping import is_uuid
from hajiri.files.parsers import json_parser
from hajiri.hiring.serializers import ApplyIn, CareersQueryIn
from hajiri.hiring.labels import EMPLOYMENT, WORK_MODE, experience_text, prose_blocks, salary_text
from hajiri.web.htmx import htmx_target, is_htmx
from hajiri.web.middleware import error_message

from . import services
from .forms import ApplicationForm

APPLY_MAX_BYTES = 6 * 1024 * 1024   # a 4 MB PDF as base64 JSON (the TS API's express.json({ limit: '6mb' }))


def _ip(request) -> str:
    return request.META.get('REMOTE_ADDR') or 'unknown'


def _no_store(res):
    res['Cache-Control'] = 'no-store'
    return res


# ---------------------------------------------------------------- JSON (public)

class PublicView(APIView):
    """No authentication and no CSRF: the public careers API answers anyone the same way."""
    authentication_classes: list = []
    permission_classes = [AllowAny]


class JobListApi(PublicView):
    @extend_schema(summary='Open jobs of a careers page (?company=CODE when several companies share the install)',
                   tags=['careers'], operation_id='careers_jobs', parameters=[CareersQueryIn],
                   responses={200: OpenApiTypes.OBJECT})
    def get(self, request, code=None):
        return _no_store(Response(services.list_jobs(request.query_params.dict(), code)))


class JobApi(PublicView):
    @extend_schema(summary='One open job with its questions', tags=['careers'], operation_id='careers_job',
                   responses={200: OpenApiTypes.OBJECT})
    def get(self, request, job_id, code=None):
        return _no_store(Response(services.get_job(job_id, code)))


class ApplyApi(PublicView):
    parser_classes = [json_parser(APPLY_MAX_BYTES)]

    @extend_schema(summary='Apply for an open job (CV as a PDF data URL)', tags=['careers'], operation_id='careers_apply',
                   request=ApplyIn, responses={201: OpenApiTypes.OBJECT})
    def post(self, request, job_id, code=None):
        return Response(services.apply(job_id, request.data, _ip(request), code), status=201)


# The same endpoints under the company's code (/api/v1/public/careers/companies/<code>/jobs...).
class CompanyJobListApi(JobListApi):
    @extend_schema(summary="Open jobs of a company's careers page", tags=['careers'], operation_id='careers_company_jobs',
                   parameters=[CareersQueryIn], responses={200: OpenApiTypes.OBJECT})
    def get(self, request, code=None):
        return super().get(request, code)


class CompanyJobApi(JobApi):
    @extend_schema(summary='One open job of the company', tags=['careers'], operation_id='careers_company_job',
                   responses={200: OpenApiTypes.OBJECT})
    def get(self, request, job_id, code=None):
        return super().get(request, job_id, code)


class CompanyApplyApi(ApplyApi):
    @extend_schema(summary='Apply for an open job of the company', tags=['careers'], operation_id='careers_company_apply',
                   request=ApplyIn, responses={201: OpenApiTypes.OBJECT})
    def post(self, request, job_id, code=None):
        return super().post(request, job_id, code)


# ---------------------------------------------------------------- pages

def _meta(job: dict) -> dict:
    return {'mode': WORK_MODE.get(job['work_mode'], job['work_mode']), 'type': EMPLOYMENT.get(job['employment_type'], job['employment_type']),
            'experience': experience_text(job.get('experience_min'), job.get('experience_max')),
            'salary': salary_text(job.get('salary_min'), job.get('salary_max'))}


def _home(code: str | None) -> str:
    """The careers page of a company, or of the install when it has one company."""
    return reverse('careers:detail', args=[code]) if code else reverse('careers:list')


def _job_url(code: str | None, job_id: str) -> str:
    return reverse('careers:company_job', args=[code, job_id]) if code else reverse('careers:detail', args=[job_id])


MODES = [('all', 'Any'), ('onsite', 'On site'), ('hybrid', 'Hybrid'), ('remote', 'Remote')]


def _list(request, code: str | None):
    query = {'q': (request.GET.get('q') or '').strip() or None,
             'workMode': request.GET.get('mode') if request.GET.get('mode') in WORK_MODE else None,
             'category': request.GET.get('category') or None}
    ctx = {'code': code, 'home': _home(code), 'mode': query['workMode'] or 'all', 'q': query['q'] or '',
           'category': query['category'] or '', 'modes': MODES}
    status = 200
    try:
        data = services.list_jobs({k: v for k, v in query.items() if v is not None}, code)
        for j in data['jobs']:
            j['meta'] = _meta(j)
            j['sub'] = ' · '.join(x for x in (j.get('department'), j.get('category')) if x)
            j['url'] = _job_url(data['company']['code'] if code else None, j['id'])
        ctx.update(data=data, company=data['company']['name'])
    except HttpError as e:
        ctx.update(data=None, error=error_message(e))
        status = 404 if isinstance(e, NotFoundError) else e.status
    if is_htmx(request) and htmx_target(request) == 'careers-jobs':
        return _no_store(render(request, 'careers/partials/site_jobs.html', ctx, status=200 if status == 404 else status))
    return _no_store(render(request, 'careers/site_list.html', ctx, status=status))


@require_GET
def job_list(request):
    """/careers, or /careers?company=CODE as the TS app linked it."""
    return _list(request, request.GET.get('company') or None)


@require_GET
def entry(request, key):
    """/careers/<code>: a company's careers page; /careers/<job id>: one job (the job names its company)."""
    if is_uuid(key):
        return _detail(request, key, request.GET.get('company') or None)
    return _list(request, key)


@require_GET
def company_job(request, code, job_id):
    """/careers/<code>/<job id>: a job of that company only."""
    return _detail(request, str(job_id), code)


def _detail_ctx(job_id: str, code: str | None) -> tuple[dict, int]:
    ctx = {'code': code, 'back': _home(code), 'job_id': job_id}
    try:
        data = services.get_job(job_id, code)
    except NotFoundError:
        return {**ctx, 'job': None}, 404
    job = data['job']
    job['meta'] = _meta(job)
    ctx.update(job=job, company=data['company']['name'], description=prose_blocks(job.get('description')),
               requirements=prose_blocks(job.get('requirements')),
               sub=' · '.join(x for x in (data['company']['name'], job.get('department')) if x))
    return ctx, 200


def _detail(request, job_id: str, code: str | None):
    ctx, status = _detail_ctx(job_id, code)
    return _no_store(render(request, 'careers/detail.html', {**ctx, 'form': {}, 'answers': {}}, status=status))


@require_POST
def apply(request, job_id):
    """The application form (HTMX, multipart): the confirmation, or the form again with the problem shown above Submit.
    ?company=CODE keeps the application to that company's job."""
    code = request.GET.get('company') or None
    ctx, _ = _detail_ctx(job_id, code)
    if not ctx.get('job'):
        return render(request, 'careers/partials/apply.html', {**ctx, 'error': services.GONE}, status=422)
    form = ApplicationForm(request.POST, request.FILES, ctx['job']['questions'])
    ctx.update(form=form.typed(), answers=form.answers(), consent=form.consented)
    try:
        filename, cv = form.cv_upload()
        r = services.apply(job_id, form.body(filename), _ip(request), code, cv_upload=cv)
    except HttpError as e:
        return render(request, 'careers/partials/apply.html', {**ctx, 'error': error_message(e)}, status=422)
    return render(request, 'careers/partials/apply.html', {**ctx, 'done': r['message']})
