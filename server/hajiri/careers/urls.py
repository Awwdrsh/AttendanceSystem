from django.urls import path

from . import views

urlpatterns = [
    path('careers', views.job_list, name='list'),
    path('careers/<str:job_id>', views.job_detail, name='detail'),
    path('_/careers/<str:job_id>/apply', views.apply, name='apply'),
    # Public JSON (no session): company websites and job boards.
    path('api/v1/public/careers/jobs', views.api_jobs, name='api_jobs'),
    path('api/v1/public/careers/jobs/<str:job_id>', views.api_job, name='api_job'),
    path('api/v1/public/careers/jobs/<str:job_id>/apply', views.api_apply, name='api_apply'),
]
