from django.urls import path

from hajiri.web.views import spa

from . import views

urlpatterns = [
    # The public careers site is the React app (CareersPage.jsx). These names stay so hiring.services.careers_url can
    # build the links it hands out (/careers, /careers/<company code or job id>, /careers/<code>/<job id>).
    path('careers', spa, name='list'),
    path('careers/<str:key>', spa, name='detail'),
    path('careers/<str:code>/<uuid:job_id>', spa, name='company_job'),
    # Public JSON (no session): company websites and job boards.
    path('api/v1/public/careers/jobs', views.JobListApi.as_view(), name='api_jobs'),
    path('api/v1/public/careers/jobs/<str:job_id>', views.JobApi.as_view(), name='api_job'),
    path('api/v1/public/careers/jobs/<str:job_id>/apply', views.ApplyApi.as_view(), name='api_apply'),
    path('api/v1/public/careers/companies/<str:code>/jobs', views.CompanyJobListApi.as_view(), name='api_company_jobs'),
    path('api/v1/public/careers/companies/<str:code>/jobs/<str:job_id>', views.CompanyJobApi.as_view(), name='api_company_job'),
    path('api/v1/public/careers/companies/<str:code>/jobs/<str:job_id>/apply', views.CompanyApplyApi.as_view(),
         name='api_company_apply'),
]
