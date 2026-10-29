from django.urls import path

# JSON API (TS: /api/v1/learning/...), called by the React app with the cookie session.
from . import api

_API = 'api/v1/learning/'
urlpatterns = [
    path(f'{_API}courses', api.CoursesView.as_view(), name='api-courses'),
    path(f'{_API}courses/<str:course_id>', api.CourseView.as_view(), name='api-course'),
    path(f'{_API}courses/<str:course_id>/questions', api.CourseQuestionsView.as_view(), name='api-course-questions'),
    path(f'{_API}courses/<str:course_id>/sessions', api.CourseSessionsView.as_view(), name='api-course-sessions'),
    path(f'{_API}courses/<str:course_id>/assign-mandatory', api.AssignMandatoryView.as_view(), name='api-course-assign-mandatory'),
    path(f'{_API}sessions/<str:session_id>', api.SessionView.as_view(), name='api-session'),
    path(f'{_API}enrollments', api.EnrollmentsView.as_view(), name='api-enrollments'),
    path(f'{_API}enrollments/self', api.SelfEnrollView.as_view(), name='api-enrollments-self'),
    path(f'{_API}enrollments/assign', api.AssignView.as_view(), name='api-enrollments-assign'),
    path(f'{_API}enrollments/<str:enrollment_id>/cancel', api.CancelView.as_view(), name='api-enrollment-cancel'),
    path(f'{_API}enrollments/<str:enrollment_id>/attendance', api.AttendanceView.as_view(), name='api-enrollment-attendance'),
    path(f'{_API}enrollments/<str:enrollment_id>/complete', api.CompleteView.as_view(), name='api-enrollment-complete'),
    path(f'{_API}enrollments/<str:enrollment_id>/assessment', api.AssessmentView.as_view(), name='api-enrollment-assessment'),
    path(f'{_API}enrollments/<str:enrollment_id>/attempts', api.AttemptsView.as_view(), name='api-enrollment-attempts'),
    path(f'{_API}enrollments/<str:enrollment_id>/feedback', api.FeedbackView.as_view(), name='api-enrollment-feedback'),
    path(f'{_API}enrollments/<str:enrollment_id>/certificate', api.CertificateView.as_view(), name='api-enrollment-certificate'),
    path(f'{_API}enrollments/<str:enrollment_id>/certificate-file', api.CertificateFileView.as_view(),
         name='api-enrollment-certificate-file'),
    path(f'{_API}expiring', api.ExpiringView.as_view(), name='api-expiring'),
    path(f'{_API}reports/cost', api.CostReportView.as_view(), name='api-cost-report'),
]
