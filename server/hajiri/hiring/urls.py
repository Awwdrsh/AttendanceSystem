from django.urls import path

from . import views

urlpatterns = [
    # Hub tabs (HiringHub in pages/hubs.tsx)
    path('hiring', views.jobs, name='jobs'),
    path('hiring/pipeline', views.pipeline, name='pipeline'),
    path('hiring/interviews', views.interviews, name='interviews'),
    path('hiring/requisitions', views.requisitions, name='requisitions'),
    # Jobs
    path('_/hiring/jobs/new', views.job_new, name='job_new'),
    path('_/hiring/jobs/<uuid:id>/edit', views.job_edit, name='job_edit'),
    path('_/hiring/jobs/<uuid:id>/publish', views.job_publish, name='job_publish'),
    path('_/hiring/jobs/<uuid:id>/close', views.job_close, name='job_close'),
    path('_/hiring/jobs/<uuid:id>/candidates/new', views.candidate_new, name='candidate_new'),
    path('_/hiring/policy', views.policy, name='policy'),
    # Requisitions
    path('_/hiring/requisitions/new', views.requisition_new, name='requisition_new'),
    path('_/hiring/requisitions/<uuid:id>/cancel', views.requisition_cancel, name='requisition_cancel'),
    # Applications
    path('_/hiring/applications/<uuid:id>', views.candidate, name='candidate'),
    path('_/hiring/applications/<uuid:id>/cv', views.cv, name='cv'),
    path('_/hiring/applications/<uuid:id>/export', views.export, name='export'),
    path('_/hiring/applications/<uuid:id>/update', views.update, name='update'),
    path('_/hiring/applications/<uuid:id>/move', views.move, name='move'),
    path('_/hiring/applications/<uuid:id>/reject', views.reject, name='reject'),
    path('_/hiring/applications/<uuid:id>/reopen', views.reopen, name='reopen'),
    path('_/hiring/applications/<uuid:id>/notes', views.note, name='note'),
    path('_/hiring/applications/<uuid:id>/interviews/new', views.interview_new, name='interview_new'),
    path('_/hiring/applications/<uuid:id>/offers/new', views.offer_new, name='offer_new'),
    path('_/hiring/applications/<uuid:id>/hire', views.hire, name='hire'),
    # Interviews and offers
    path('_/hiring/interviews/<uuid:id>/update', views.interview_update, name='interview_update'),
    path('_/hiring/interviews/<uuid:id>/scorecard', views.scorecard, name='scorecard'),
    path('_/hiring/offers/<uuid:id>/letter', views.offer_letter, name='offer_letter'),
    path('_/hiring/offers/<uuid:id>/respond', views.offer_respond, name='offer_respond'),
    path('_/hiring/offers/<uuid:id>/withdraw', views.offer_withdraw, name='offer_withdraw'),
]
