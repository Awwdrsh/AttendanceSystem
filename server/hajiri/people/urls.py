from django.urls import path

# JSON API for the React app and Bearer-token clients (TS: /api/v1/people/...). Ids are <str:...> so the service
# answers a malformed one with the TS 400 "Invalid UUID"; the fixed paths come before /people/<id>/... .
from . import api

P = 'api/v1/people'
urlpatterns = [
    path(f'{P}/directory', api.DirectoryView.as_view(), name='api_directory'),
    path(f'{P}/summary', api.SummaryView.as_view(), name='api_summary'),
    path(f'{P}/org-chart', api.OrgChartView.as_view(), name='api_org_chart'),
    path(f'{P}/structure', api.StructureView.as_view(), name='api_structure'),
    path(f'{P}/structure/branches', api.BranchesView.as_view(), name='api_branches'),
    path(f'{P}/structure/branches/<str:id>', api.BranchView.as_view(), name='api_branch'),
    path(f'{P}/structure/units', api.UnitsView.as_view(), name='api_units'),
    path(f'{P}/structure/units/<str:id>', api.UnitView.as_view(), name='api_unit'),
    path(f'{P}/structure/designations', api.DesignationsView.as_view(), name='api_designations'),
    path(f'{P}/structure/designations/<str:id>', api.DesignationView.as_view(), name='api_designation'),
    path(f'{P}/policy', api.PolicyView.as_view(), name='api_policy'),
    path(f'{P}/me/change-requests', api.MyChangeRequestsView.as_view(), name='api_my_requests'),
    path(f'{P}/me/change-requests/<str:id>/cancel', api.MyChangeRequestCancelView.as_view(), name='api_my_request_cancel'),
    path(f'{P}/me/avatar', api.MyAvatarView.as_view(), name='api_my_avatar'),
    path(f'{P}/change-requests', api.ChangeRequestsView.as_view(), name='api_requests'),
    path(f'{P}/change-requests/<str:id>', api.ChangeRequestView.as_view(), name='api_request'),
    path(f'{P}/change-requests/<str:id>/proof', api.ChangeRequestProofView.as_view(), name='api_request_proof'),
    path(f'{P}/<str:id>/card', api.CardView.as_view(), name='api_card'),
    path(f'{P}/<str:id>/avatar', api.AvatarView.as_view(), name='api_avatar'),
    path(f'{P}/<str:id>/profile', api.ProfileView.as_view(), name='api_profile'),
    path(f'{P}/<str:id>/profile/<str:section>', api.ProfileSectionView.as_view(), name='api_profile_section'),
    path(f'{P}/<str:id>/history', api.HistoryView.as_view(), name='api_history'),
    path(f'{P}/<str:id>/career', api.CareerView.as_view(), name='api_career'),
    path(f'{P}/<str:id>/career/<str:event_id>/cancel', api.CareerCancelView.as_view(), name='api_career_cancel'),
]
