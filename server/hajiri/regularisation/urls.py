from django.urls import path

from . import api, views

urlpatterns = [
    path('attendance/fix', views.fix_punch, name='fix'),
    path('_/regularisation/fix', views.fix_punch_submit, name='fix_submit'),
    path('_/regularisation/new', views.correction_new, name='new'),
    path('_/regularisation/wfh', views.wfh_new, name='wfh'),
    path('_/regularisation/list', views.request_list, name='list'),
    path('_/regularisation/<uuid:id>/cancel', views.cancel, name='cancel'),
    path('_/regularisation/offline', api.OfflineFlushView.as_view(), name='offline_flush'),
    # JSON for API clients and the page script (DRF views).
    path('api/v1/regularisation', api.RequestsView.as_view(), name='api_requests'),
    path('api/v1/regularisation/<str:id>/cancel', api.CancelView.as_view(), name='api_cancel'),
]
