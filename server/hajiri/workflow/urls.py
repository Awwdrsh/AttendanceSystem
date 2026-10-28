from django.urls import path

from . import api, views

urlpatterns = [
    # pages (React routes)
    path('approvals', views.approvals, name='approvals'),
    path('leave/history', views.history, name='history'),
    path('settings/approvals', views.flows_page, name='flows'),
    # addresses from the earlier layout (App.tsx LEGACY)
    # inbox fragments and decisions
    path('_/workflow/inbox', views.inbox_partial, name='inbox'),
    path('_/workflow/preview', views.preview, name='preview'),
    path('_/workflow/bulk-approve', views.bulk_approve, name='bulk_approve'),
    path('_/workflow/delegates', views.delegates, name='delegates'),
    path('_/workflow/delegates/new', views.delegate_new, name='delegate_new'),
    path('_/workflow/delegates/button', views.delegate_button, name='delegate_button'),
    path('_/workflow/delegates/<uuid:id>/cancel', views.delegate_cancel, name='delegate_cancel'),
    # flow set-up
    path('_/workflow/steps', views.steps_editor, name='steps_editor'),
    path('_/workflow/defaults/<str:object_type>', views.set_default, name='set_default'),
    path('_/workflow/flows/new', views.flow_new, name='flow_new'),
    path('_/workflow/flows/<uuid:id>/edit', views.flow_edit, name='flow_edit'),
    path('_/workflow/flows/<uuid:id>/active', views.flow_active, name='flow_active'),
    path('_/workflow/flows/<uuid:id>/delete', views.flow_delete, name='flow_delete'),
    path('_/workflow/groups/new', views.group_form, name='group_new'),
    path('_/workflow/groups/<uuid:id>/edit', views.group_form, name='group_edit'),
    path('_/workflow/groups/<uuid:id>/delete', views.group_delete, name='group_delete'),
    path('_/workflow/verify', views.verify, name='verify'),
    path('_/workflow/<uuid:id>', views.detail, name='detail'),
    path('_/workflow/<uuid:id>/approve', views.approve, name='approve'),
    path('_/workflow/<uuid:id>/reject', views.reject, name='reject'),
    # JSON for Bearer clients (TS paths; DRF views in api.py)
    path('api/v1/workflow/inbox', api.InboxView.as_view(), name='api_inbox'),
    path('api/v1/workflow/preview', api.PreviewView.as_view(), name='api_preview'),
    path('api/v1/workflow/bulk-approve', api.BulkApproveView.as_view(), name='api_bulk_approve'),
    path('api/v1/workflow/<str:id>', api.DetailView.as_view(), name='api_detail'),
    path('api/v1/workflow/<str:id>/approve', api.ApproveView.as_view(), name='api_approve'),
    path('api/v1/workflow/<str:id>/reject', api.RejectView.as_view(), name='api_reject'),
]
