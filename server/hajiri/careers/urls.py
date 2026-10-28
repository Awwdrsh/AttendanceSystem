from django.urls import path

from . import views

urlpatterns = [
    # Pages: one careers site per company. /careers/<key> is a company code or (the TS link) a job id.
    path('careers', views.job_list, name='list'),
    path('careers/<str:key>', views.entry, name='detail'),
    path('careers/<str:code>/<uuid:job_id>', views.company_job, name='company_job'),
    path('_/careers/<str:job_id>/apply', views.apply, name='apply'),
    # Public JSON (no session): company websites and job boards.
    path('api/v1/public/careers/jobs', views.JobListApi.as_view(), name='api_jobs'),
    path('api/v1/public/careers/jobs/<str:job_id>', views.JobApi.as_view(), name='api_job'),
    path('api/v1/public/careers/jobs/<str:job_id>/apply', views.ApplyApi.as_view(), name='api_apply'),
    path('api/v1/public/careers/companies/<str:code>/jobs', views.CompanyJobListApi.as_view(), name='api_company_jobs'),
    path('api/v1/public/careers/companies/<str:code>/jobs/<str:job_id>', views.CompanyJobApi.as_view(), name='api_company_job'),
    path('api/v1/public/careers/companies/<str:code>/jobs/<str:job_id>/apply', views.CompanyApplyApi.as_view(),
         name='api_company_apply'),
]
