"""
Learning JSON API: the TS routes under /api/v1/learning (apps/api/src/modules/learning/routes.ts at 65a9079), which
the React app calls (frontend/src/api/performance.js `learning`). Same paths, methods, bodies, JSON shapes and status
codes.

DRF views with the project defaults (hajiri.core.api): IsSignedIn, the TS error shape and JsonRenderer, plus the
Learning feature switch (checked before any role, as featureGate was mounted ahead of requireRole). They authenticate
with SameOriginAuthentication: the React app sends the session cookie with an Origin check and never a CSRF token.
Rules and validation stay in services.py; every view works in the active company.
"""
from __future__ import annotations

from django.http import HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import FeatureEnabled, HasRole, IsSignedIn, SameOriginAuthentication
from hajiri.files.store import stored_file_response

from . import serializers as s
from . import services

OK = {'ok': True}
HR = HasRole('hr')


class _View(APIView):
    """Signed in with the Learning feature on; the methods in `hr_methods` also need HR (requireRole('hr'))."""
    authentication_classes = [SameOriginAuthentication]
    permission_classes = [IsSignedIn, FeatureEnabled('learning')]
    hr_methods: tuple[str, ...] = ()

    def get_permissions(self):
        perms = super().get_permissions()
        if self.request.method in self.hr_methods:
            perms.append(HR())
        return perms


class _HrView(_View):
    hr_methods = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')


def _created(data) -> Response:
    return Response(data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------- catalogue

class CoursesView(_View):
    hr_methods = ('POST',)

    @extend_schema(operation_id='learning_courses_list', responses=dict)
    def get(self, request):
        return Response({'courses': services.list_courses(request.user)})

    @extend_schema(operation_id='learning_courses_create', request=s.CourseIn, responses={201: dict})
    def post(self, request):
        return _created({'id': services.create_course(request.user, request.data)})


class CourseView(_View):
    hr_methods = ('PATCH',)

    @extend_schema(operation_id='learning_course_get', responses=dict)
    def get(self, request, course_id):
        return Response(services.course_detail(request.user, course_id))

    @extend_schema(operation_id='learning_course_patch', request=s.CourseIn, responses=dict)
    def patch(self, request, course_id):
        services.update_course(request.user, course_id, request.data)
        return Response(OK)


class CourseQuestionsView(_HrView):
    """Replace the course's multiple-choice assessment."""

    @extend_schema(operation_id='learning_course_questions', request=s.QuestionsIn, responses=dict)
    def put(self, request, course_id):
        services.save_questions(request.user, course_id, request.data)
        return Response(OK)


class CourseSessionsView(_HrView):
    @extend_schema(operation_id='learning_session_add', request=s.SessionIn, responses={201: dict})
    def post(self, request, course_id):
        return _created({'id': services.add_session(request.user, course_id, request.data)})


class SessionView(_HrView):
    @extend_schema(operation_id='learning_session_patch', request=s.SessionIn, responses=dict)
    def patch(self, request, session_id):
        services.update_session(request.user, session_id, request.data)
        return Response(OK)


class AssignMandatoryView(_HrView):
    @extend_schema(operation_id='learning_assign_mandatory', request=None, responses=dict)
    def post(self, request, course_id):
        return Response({'ok': True, 'added': services.assign_mandatory(request.user, course_id)})


# ---------------------------------------------------------------- enrolments

class EnrollmentsView(_View):
    @extend_schema(operation_id='learning_enrollments_list', responses=dict)
    def get(self, request):
        return Response({'enrollments': services.list_enrollments(request.user, request.query_params.dict())})


class SelfEnrollView(_View):
    """Ask to join a course; the manager approves through the approvals inbox."""

    @extend_schema(operation_id='learning_self_enroll', request=s.SelfEnrollIn, responses={201: dict})
    def post(self, request):
        return _created({'id': services.self_enroll(request.user, request.data)})


class AssignView(_View):
    """HR assigns anyone; a manager assigns their team."""

    @extend_schema(operation_id='learning_assign', request=s.AssignIn, responses={201: dict})
    def post(self, request):
        return _created(services.assign(request.user, request.data))


class CancelView(_View):
    @extend_schema(operation_id='learning_enrollment_cancel', request=None, responses=dict)
    def post(self, request, enrollment_id):
        services.cancel(request.user, enrollment_id)
        return Response(OK)


class AttendanceView(_View):
    @extend_schema(operation_id='learning_enrollment_attendance', request=s.AttendanceIn, responses=dict)
    def post(self, request, enrollment_id):
        services.record_attendance(request.user, enrollment_id, request.data)
        return Response(OK)


class CompleteView(_View):
    @extend_schema(operation_id='learning_enrollment_complete', request=None, responses=dict)
    def post(self, request, enrollment_id):
        services.complete(request.user, enrollment_id)
        return Response(OK)


class AssessmentView(_View):
    @extend_schema(operation_id='learning_enrollment_assessment', responses=dict)
    def get(self, request, enrollment_id):
        return Response(services.assessment(request.user, enrollment_id))


class AttemptsView(_View):
    @extend_schema(operation_id='learning_enrollment_attempt', request=s.AttemptIn, responses={201: dict})
    def post(self, request, enrollment_id):
        return _created(services.attempt(request.user, enrollment_id, request.data))


class FeedbackView(_View):
    @extend_schema(operation_id='learning_enrollment_feedback', request=s.CourseFeedbackIn, responses=dict)
    def post(self, request, enrollment_id):
        services.course_feedback(request.user, enrollment_id, request.data)
        return Response(OK)


class CertificateView(_View):
    """POST attaches an uploaded certificate; GET prints the system's certificate of completion (HTML)."""

    @extend_schema(operation_id='learning_certificate_attach', request=s.CertificateIn, responses=dict)
    def post(self, request, enrollment_id):
        services.attach_certificate(request.user, enrollment_id, request.data)
        return Response(OK)

    @extend_schema(operation_id='learning_certificate_print', responses={(200, 'text/html'): str})
    def get(self, request, enrollment_id):
        res = HttpResponse(services.certificate_html(request.user, enrollment_id), content_type='text/html; charset=utf-8')
        res['Cache-Control'] = 'private, no-store'
        return res


class CertificateFileView(_View):
    @extend_schema(operation_id='learning_certificate_file', responses={(200, 'application/octet-stream'): bytes})
    def get(self, request, enrollment_id):
        f = services.certificate_file(request.user, enrollment_id)
        return stored_file_response(f, download=request.query_params.get('download') == '1')


# ---------------------------------------------------------------- reports

class ExpiringView(_HrView):
    @extend_schema(operation_id='learning_expiring', responses=dict)
    def get(self, request):
        return Response({'enrollments': services.expiring(request.user, request.query_params.get('days', ''))})


class CostReportView(_HrView):
    @extend_schema(operation_id='learning_cost_report', responses=dict)
    def get(self, request):
        return Response(services.cost_report(request.user, request.query_params.dict()))
