"""
Public careers site: /careers lists open jobs, /careers/<job id> shows one with the application form (CareersPage.tsx).
No sign-in. The JSON endpoints under /api/v1/public/careers/ stay for company websites and job boards that list or
post to the careers page from elsewhere (the TS routes, same shapes).
"""
from __future__ import annotations

import base64
from urllib.parse import quote

from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from hajiri.core.errors import HttpError, NotFoundError
from hajiri.core.http import api_view, json_response, read_json
from hajiri.hiring.labels import EMPLOYMENT, WORK_MODE, experience_text, prose_blocks, salary_text
from hajiri.web.htmx import is_htmx
from hajiri.web.middleware import error_message

from . import services

CV_MAX = 4 * 1024 * 1024


def _ip(request) -> str:
    return request.META.get('REMOTE_ADDR') or 'unknown'


def _no_store(res):
    res['Cache-Control'] = 'no-store'
    return res


# ---------------------------------------------------------------- JSON (public)

@api_view(methods=('GET',), auth=False)
def api_jobs(request):
    return _no_store(json_response(services.list_jobs(request.GET.dict())))


@api_view(methods=('GET',), auth=False)
def api_job(request, job_id):
    return _no_store(json_response(services.get_job(job_id)))


@api_view(methods=('POST',), auth=False)
def api_apply(request, job_id):
    body = read_json(request, max_bytes=6 * 1024 * 1024)
    return json_response(services.apply(job_id, body, _ip(request)), status=201)


# ---------------------------------------------------------------- pages

def _meta(job: dict) -> dict:
    return {'mode': WORK_MODE.get(job['work_mode'], job['work_mode']), 'type': EMPLOYMENT.get(job['employment_type'], job['employment_type']),
            'experience': experience_text(job.get('experience_min'), job.get('experience_max')),
            'salary': salary_text(job.get('salary_min'), job.get('salary_max'))}


def _suffix(code: str | None) -> str:
    return f'?company={quote(code, safe="")}' if code else ''


@require_GET
def job_list(request):
    code = request.GET.get('company') or None
    query = {'company': code, 'q': (request.GET.get('q') or '').strip() or None,
             'workMode': request.GET.get('mode') if request.GET.get('mode') in WORK_MODE else None,
             'category': request.GET.get('category') or None}
    ctx = {'code': code, 'suffix': _suffix(code), 'mode': query['workMode'] or 'all', 'q': query['q'] or '',
           'category': query['category'] or '', 'modes': [('all', 'Any'), ('onsite', 'On site'), ('hybrid', 'Hybrid'), ('remote', 'Remote')]}
    status = 200
    try:
        data = services.list_jobs({k: v for k, v in query.items() if v is not None})
        for j in data['jobs']:
            j['meta'] = _meta(j)
            j['sub'] = ' · '.join(x for x in (j.get('department'), j.get('category')) if x)
        ctx.update(data=data, company=data['company']['name'])
    except HttpError as e:
        ctx.update(data=None, error=error_message(e))
        status = 404 if isinstance(e, NotFoundError) else e.status
    if is_htmx(request) and request.headers.get('HX-Target') == 'careers-jobs':
        return _no_store(render(request, 'careers/partials/jobs.html', ctx, status=200 if status == 404 else status))
    return _no_store(render(request, 'careers/list.html', ctx, status=status))


def _detail_ctx(request, job_id: str) -> tuple[dict, int]:
    code = request.GET.get('company') or None
    ctx = {'code': code, 'back': f'/careers{_suffix(code)}', 'job_id': job_id}
    try:
        data = services.get_job(job_id)
    except NotFoundError:
        return {**ctx, 'job': None}, 404
    job = data['job']
    job['meta'] = _meta(job)
    ctx.update(job=job, company=data['company']['name'], description=prose_blocks(job.get('description')),
               requirements=prose_blocks(job.get('requirements')),
               sub=' · '.join(x for x in (data['company']['name'], job.get('department')) if x))
    return ctx, 200


@require_GET
def job_detail(request, job_id):
    ctx, status = _detail_ctx(request, job_id)
    return _no_store(render(request, 'careers/detail.html', {**ctx, 'form': {}, 'answers': {}}, status=status))


def _num(v: str):
    v = (v or '').strip()
    if v == '':
        return None
    try:
        f = float(v)
    except ValueError:
        return v   # the schema reports it
    return int(f) if f.is_integer() else f


@require_POST
def apply(request, job_id):
    """The application form (HTMX, multipart): the confirmation, or the form again with the problem shown above Submit."""
    ctx, status = _detail_ctx(request, job_id)
    if not ctx.get('job'):
        return render(request, 'careers/partials/apply.html', {**ctx, 'error': services.GONE}, status=422)
    job = ctx['job']
    p = request.POST
    answers = {}
    for q in job['questions']:
        raw = p.get(f"answer_{q['id']}", '')
        if q['kind'] == 'number':
            v = _num(raw)
            if v is not None:
                answers[q['id']] = v
        elif raw.strip() != '':
            answers[q['id']] = raw
    form = {k: p.get(k, '') for k in ('name', 'email', 'phone', 'experienceYears', 'currentEmployer', 'coverNote')}
    ctx.update(form=form, answers=answers, consent=p.get('consent') == '1')
    cv = request.FILES.get('cv')
    try:
        if not cv:
            raise HttpError('Please attach your CV as a PDF.')
        if cv.size > CV_MAX:
            raise HttpError('Your CV is larger than 4 MB. Please send a smaller PDF.')
        if cv.content_type != 'application/pdf':
            raise HttpError('Please attach your CV as a PDF.')
        body = {
            'name': form['name'].strip(), 'email': form['email'].strip(), 'phone': form['phone'].strip(),
            'experienceYears': _num(form['experienceYears']), 'currentEmployer': form['currentEmployer'].strip() or None,
            'coverNote': form['coverNote'].strip() or None, 'answers': answers, 'cvFilename': cv.name,
            'cvDataUrl': f"data:application/pdf;base64,{base64.b64encode(cv.read()).decode()}",
            'consent': p.get('consent') == '1',
        }
        if p.get('website'):
            body['website'] = p['website']
        r = services.apply(job_id, body, _ip(request))
    except HttpError as e:
        return render(request, 'careers/partials/apply.html', {**ctx, 'error': error_message(e)}, status=422)
    return render(request, 'careers/partials/apply.html', {**ctx, 'done': r['message']})

