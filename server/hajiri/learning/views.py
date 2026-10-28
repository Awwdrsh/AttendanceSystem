"""
Learning pages: My learning (/me/growth/learning) and the Learning tab of the Performance hub (/performance/learning),
rebuilt from pages/performance/MyLearningPage.tsx and LearningAdminPage.tsx, plus the printable certificate and the
uploaded certificate file.
"""
from __future__ import annotations

from datetime import datetime, timezone

from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import has_role
from hajiri.core.errors import ForbiddenError
from hajiri.files.store import stored_file_response
from hajiri.performance.goals import colleagues
from hajiri.performance.views import _pills, done, feature, staff
from hajiri.web.format import add_days, fmt_money, today_npt

from . import forms as f
from . import services as lrn

ENROLL_OPTIONS = [('pending_approval', 'Awaiting approval'), ('enrolled', 'Enrolled'), ('completed', 'Completed'),
                  ('rejected', 'Not approved'), ('cancelled', 'Cancelled'), ('no_show', 'Did not attend')]
OPEN = ('pending_approval', 'enrolled')


def _require_hr(user) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


@require_GET
@feature('learning')
def my_learning(request):
    user = request.hajiri_user
    view = 'catalogue' if request.GET.get('view') == 'catalogue' else 'mine'
    mine = lrn.list_enrollments(user, {'scope': 'mine'})
    ctx = {'view': view, 'views': _pills(view, [('mine', 'My learning'), ('catalogue', 'Catalogue')]), 'rows': mine}
    if view == 'catalogue':
        courses = lrn.list_courses(user)
        for c in courses:
            c['open'] = next((e for e in mine if e['course_id'] == c['id'] and e['status'] in OPEN), None)
        ctx['courses'] = courses
    return render(request, 'learning/my_learning.html', ctx)


ADMIN_VIEWS = [('courses', 'Courses'), ('enrollments', 'Enrolments'), ('expiring', 'Certificates'), ('cost', 'Spend')]


@require_GET
@staff('learning')
def admin(request):
    user = request.hajiri_user
    hr = has_role(user, 'hr')
    options = ADMIN_VIEWS if hr else [('courses', 'Courses'), ('enrollments', 'My team')]
    view = request.GET.get('view') if request.GET.get('view') in dict(options) else 'courses'
    ctx = {'hr': hr, 'view': view, 'views': _pills(view, options)}
    if view == 'courses':
        ctx['courses'] = lrn.list_courses(user)
    elif view == 'enrollments':
        filters = f.EnrollmentFilterForm(request.GET).query()
        ctx.update(enrollments=lrn.list_enrollments(user, {'scope': 'all' if hr else 'team', **filters}), courses=lrn.list_courses(user),
                   course=filters.get('courseId'), status=filters.get('status'), statuses=ENROLL_OPTIONS)
    elif view == 'expiring':
        days = request.GET.get('days') if request.GET.get('days') in ('30', '60', '180') else '60'
        ctx.update(enrollments=lrn.expiring(user, days),
                   days=_pills(days, [('30', '30 days'), ('60', '60 days'), ('180', '6 months')], 'days', '&view=expiring'))
    else:
        frm = request.GET.get('from') or add_days(today_npt(), -365)
        to = request.GET.get('to') or today_npt()
        rep = lrn.cost_report(user, {'from': frm, 'to': to})
        seats = sum(x['seats'] for x in rep['byCourse'])
        ctx.update(rep=rep, frm=frm, to=to, stats=[{'label': 'Total spend (NPR)', 'value': fmt_money(rep['total'], currency=False)},
                                                   {'label': 'Seats', 'value': seats}])
    return render(request, 'learning/admin.html', ctx)


# ---------------------------------------------------------------- catalogue (HR)

def _course_drawer(request, course_id, status=200):
    d = lrn.course_detail(request.hajiri_user, course_id)
    return render(request, 'learning/partials/course_form.html', {
        'c': d['course'], 'd': d, 'session_date': add_days(today_npt(), 7),
        'questions': [{'prompt': q['prompt'], 'options': q['options'], 'correctIndex': q.get('correct_index', 0)} for q in d['questions']]},
        status=status)


@require_http_methods(['GET', 'POST'])
@staff('learning')
def course_new(request):
    if request.method == 'GET':
        _require_hr(request.hajiri_user)
        return render(request, 'learning/partials/course_form.html', {
            'c': {'delivery': 'classroom', 'source': 'internal', 'cost': '0', 'mandatory': False, 'mandatory_for': [], 'active': True}})
    lrn.create_course(request.hajiri_user, f.CourseForm(request.POST).body())
    return done(request, 'Course saved.', close=True)


@require_http_methods(['GET', 'POST'])
@staff('learning')
def course_edit(request, id):
    if request.method == 'GET':
        _require_hr(request.hajiri_user)
        return _course_drawer(request, str(id))
    lrn.update_course(request.hajiri_user, str(id), f.CourseForm(request.POST).body())
    return done(request, 'Course saved.', html=_course_drawer(request, str(id)).content)


@require_POST
@staff('learning')
def course_mandatory(request, id):
    n = lrn.assign_mandatory(request.hajiri_user, str(id))
    return done(request, f'{n} people enrolled.')


@require_POST
@staff('learning')
def course_questions(request, id):
    lrn.save_questions(request.hajiri_user, str(id), f.QuestionsForm(request.POST).body())
    return done(request, 'Assessment saved.', html=_course_drawer(request, str(id)).content)


@require_POST
@staff('learning')
def session_add(request, id):
    lrn.add_session(request.hajiri_user, str(id), f.SessionForm(request.POST).body())
    return done(request, 'Session added.', html=_course_drawer(request, str(id)).content)


@require_POST
@staff('learning')
def session_status(request, id):
    course_id = lrn.update_session(request.hajiri_user, str(id), {'status': f.SessionStatusForm(request.POST).cleaned()['status']})
    return done(request, '', html=_course_drawer(request, course_id).content)


@require_http_methods(['GET', 'POST'])
@staff('learning')
def assign(request, id):
    user = request.hajiri_user
    if request.method == 'GET':
        hr = has_role(user, 'hr')
        d = lrn.course_detail(user, str(id))
        people = [c for c in colleagues(user) if hr or c['manager_id'] == user.employee_id]
        return render(request, 'learning/partials/assign.html', {
            'c': d['course'], 'people': people, 'sessions': [x for x in d['sessions'] if x['status'] == 'scheduled']})
    r = lrn.assign(user, f.AssignForm(request.POST).body(str(id)))
    skipped = f", {r['skipped']} already enrolled" if r['skipped'] else ''
    return done(request, f"{r['added']} enrolled{skipped}.", close=True)


@require_http_methods(['GET', 'POST'])
@feature('learning')
def join(request, id):
    user = request.hajiri_user
    if request.method == 'GET':
        d = lrn.course_detail(user, str(id))
        now = datetime.now(timezone.utc)
        sessions = [x for x in d['sessions'] if x['status'] == 'scheduled' and x['starts_at'] > now]
        for x in sessions:
            x['left'] = max(0, x['capacity'] - x['taken']) if x['capacity'] else None
        return render(request, 'learning/partials/join.html', {'c': d['course'], 'sessions': sessions})
    lrn.self_enroll(user, f.JoinForm(request.POST).body(str(id)))
    resp = done(request, 'Sent to your manager for approval.', close=True)
    resp.headers['HX-Redirect'] = reverse('learning:mine')
    return resp


# ---------------------------------------------------------------- enrolments

@require_POST
@feature('learning')
def cancel(request, id):
    lrn.cancel(request.hajiri_user, str(id))
    return done(request, 'Cancelled.')


@require_POST
@feature('learning')
def attendance(request, id):
    body = f.AttendanceForm(request.POST).body()
    lrn.record_attendance(request.hajiri_user, str(id), body)
    return done(request, 'Attendance recorded.' if body['attended'] else 'Marked absent.')


@require_POST
@feature('learning')
def complete(request, id):
    lrn.complete(request.hajiri_user, str(id))
    return done(request, 'Marked complete.')


@require_http_methods(['GET', 'POST'])
@feature('learning')
def assessment(request, id):
    user = request.hajiri_user
    a = lrn.assessment(user, str(id))
    if request.method == 'GET':
        return render(request, 'learning/partials/assessment.html', {'a': a, 'id': str(id)})
    r = lrn.attempt(user, str(id), f.AnswersForm(request.POST, len(a['questions'])).body())
    return done(request, '', html=render(request, 'learning/partials/assessment_result.html', {'r': r, 'id': str(id), 'title': a['title']}).content)


@require_http_methods(['GET', 'POST'])
@feature('learning')
def rate(request, id):
    if request.method == 'GET':
        lrn.get_enrollment(request.hajiri_user, str(id), learner_only=True)
        return render(request, 'learning/partials/rate.html', {'id': str(id), 'levels': list(enumerate(['Poor', 'Fair', 'Good', 'Very good', 'Excellent'], 1))})
    lrn.course_feedback(request.hajiri_user, str(id), f.RateForm(request.POST).body())
    return done(request, 'Thanks for the feedback.', close=True)


@require_http_methods(['GET', 'POST'])
@feature('learning')
def certificate_upload(request, id):
    if request.method == 'GET':
        lrn.get_enrollment(request.hajiri_user, str(id))
        return render(request, 'learning/partials/certificate_upload.html', {'id': str(id)})
    lrn.attach_certificate(request.hajiri_user, str(id), f.CertificateForm(request.POST).body())
    return done(request, 'Certificate added.', close=True)


@require_GET
@feature('learning')
def certificate_file(request, id):
    return stored_file_response(lrn.certificate_file(request.hajiri_user, str(id)), download=request.GET.get('download') == '1')


@require_GET
@feature('learning')
def certificate(request, id):
    resp = HttpResponse(lrn.certificate_html(request.hajiri_user, str(id)), content_type='text/html; charset=utf-8')
    resp['Cache-Control'] = 'private, no-store'
    return resp
