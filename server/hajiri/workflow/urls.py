from django.urls import path

from . import api

urlpatterns = [
    # JSON for the React app and Bearer clients (TS paths; DRF views in api.py); fixed names before /<id>
    path('api/v1/workflow/inbox', api.InboxView.as_view(), name='api_inbox'),
    path('api/v1/workflow/preview', api.PreviewView.as_view(), name='api_preview'),
    path('api/v1/workflow/bulk-approve', api.BulkApproveView.as_view(), name='api_bulk_approve'),
    path('api/v1/workflow/verify', api.VerifyView.as_view(), name='api_verify'),
    path('api/v1/workflow/flows/config', api.FlowsConfigView.as_view(), name='api_flows_config'),
    path('api/v1/workflow/flows/defaults/<str:object_type>', api.FlowDefaultView.as_view(), name='api_flow_default'),
    path('api/v1/workflow/flows', api.FlowsView.as_view(), name='api_flows'),
    path('api/v1/workflow/flows/<str:id>', api.FlowView.as_view(), name='api_flow'),
    path('api/v1/workflow/groups', api.GroupsView.as_view(), name='api_groups'),
    path('api/v1/workflow/groups/<str:id>', api.GroupView.as_view(), name='api_group'),
    path('api/v1/workflow/delegates', api.DelegatesView.as_view(), name='api_delegates'),
    path('api/v1/workflow/delegates/candidates', api.DelegateCandidatesView.as_view(), name='api_delegate_candidates'),
    path('api/v1/workflow/delegates/<str:id>', api.DelegateView.as_view(), name='api_delegate'),
    path('api/v1/workflow/<str:id>', api.DetailView.as_view(), name='api_detail'),
    path('api/v1/workflow/<str:id>/approve', api.ApproveView.as_view(), name='api_approve'),
    path('api/v1/workflow/<str:id>/reject', api.RejectView.as_view(), name='api_reject'),
]
