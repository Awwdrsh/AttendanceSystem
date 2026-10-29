from django.urls import path

# JSON API for the React app and Bearer-token clients (TS: /api/v1/hiring/...). Ids are <str:...> so the services
# answer a malformed one with the TS 400 "Invalid UUID".
from . import api

H = 'api/v1/hiring'
urlpatterns = [
    path(f'{H}/requisitions', api.RequisitionsView.as_view(), name='api_requisitions'),
    path(f'{H}/requisitions/<str:id>/cancel', api.RequisitionCancelView.as_view(), name='api_requisition_cancel'),
    path(f'{H}/jobs', api.JobsView.as_view(), name='api_jobs'),
    path(f'{H}/jobs/<str:id>', api.JobView.as_view(), name='api_job'),
    path(f'{H}/jobs/<str:id>/publish', api.JobPublishView.as_view(), name='api_job_publish'),
    path(f'{H}/jobs/<str:id>/close', api.JobCloseView.as_view(), name='api_job_close'),
    path(f'{H}/jobs/<str:id>/applications', api.JobApplicationsView.as_view(), name='api_job_applications'),
    path(f'{H}/applications/<str:id>', api.ApplicationView.as_view(), name='api_application'),
    path(f'{H}/applications/<str:id>/cv', api.ApplicationCvView.as_view(), name='api_application_cv'),
    path(f'{H}/applications/<str:id>/move', api.ApplicationMoveView.as_view(), name='api_application_move'),
    path(f'{H}/applications/<str:id>/reject', api.ApplicationRejectView.as_view(), name='api_application_reject'),
    path(f'{H}/applications/<str:id>/reopen', api.ApplicationReopenView.as_view(), name='api_application_reopen'),
    path(f'{H}/applications/<str:id>/notes', api.ApplicationNotesView.as_view(), name='api_application_notes'),
    path(f'{H}/applications/<str:id>/export', api.ApplicationExportView.as_view(), name='api_application_export'),
    path(f'{H}/applications/<str:id>/interviews', api.ApplicationInterviewsView.as_view(), name='api_application_interviews'),
    path(f'{H}/applications/<str:id>/offers', api.ApplicationOffersView.as_view(), name='api_application_offers'),
    path(f'{H}/applications/<str:id>/hire', api.ApplicationHireView.as_view(), name='api_application_hire'),
    path(f'{H}/interviews', api.InterviewsView.as_view(), name='api_interviews'),
    path(f'{H}/interviews/<str:id>', api.InterviewView.as_view(), name='api_interview'),
    path(f'{H}/interviews/<str:id>/scorecard', api.InterviewScorecardView.as_view(), name='api_interview_scorecard'),
    path(f'{H}/offers/<str:id>/letter', api.OfferLetterView.as_view(), name='api_offer_letter'),
    path(f'{H}/offers/<str:id>/respond', api.OfferRespondView.as_view(), name='api_offer_respond'),
    path(f'{H}/offers/<str:id>/withdraw', api.OfferWithdrawView.as_view(), name='api_offer_withdraw'),
    path(f'{H}/lookups', api.LookupsView.as_view(), name='api_lookups'),
    path(f'{H}/reports', api.ReportsView.as_view(), name='api_reports'),
    path(f'{H}/policy', api.PolicyView.as_view(), name='api_policy'),
]
