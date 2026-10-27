from django.urls import path

from . import views

urlpatterns = [
    path('me/growth/learning', views.my_learning, name='mine'),
    path('performance/learning', views.admin, name='admin'),
    path('_/learning/courses/new', views.course_new, name='course_new'),
    path('_/learning/courses/<uuid:id>/edit', views.course_edit, name='course_edit'),
    path('_/learning/courses/<uuid:id>/assign-mandatory', views.course_mandatory, name='course_mandatory'),
    path('_/learning/courses/<uuid:id>/questions', views.course_questions, name='course_questions'),
    path('_/learning/courses/<uuid:id>/sessions', views.session_add, name='session_add'),
    path('_/learning/courses/<uuid:id>/assign', views.assign, name='assign'),
    path('_/learning/courses/<uuid:id>/join', views.join, name='join'),
    path('_/learning/sessions/<uuid:id>/status', views.session_status, name='session_status'),
    path('_/learning/enrollments/<uuid:id>/cancel', views.cancel, name='cancel'),
    path('_/learning/enrollments/<uuid:id>/attendance', views.attendance, name='attendance'),
    path('_/learning/enrollments/<uuid:id>/complete', views.complete, name='complete'),
    path('_/learning/enrollments/<uuid:id>/assessment', views.assessment, name='assessment'),
    path('_/learning/enrollments/<uuid:id>/feedback', views.rate, name='rate'),
    path('_/learning/enrollments/<uuid:id>/certificate/upload', views.certificate_upload, name='certificate_upload'),
    path('_/learning/enrollments/<uuid:id>/certificate-file', views.certificate_file, name='certificate_file'),
    path('_/learning/enrollments/<uuid:id>/certificate', views.certificate, name='certificate'),
]
