from django.urls import path

from . import views

urlpatterns = [
    path('attendance/fix', views.fix_punch, name='fix'),
    path('_/regularisation/fix', views.fix_punch_submit, name='fix_submit'),
    path('_/regularisation/new', views.correction_new, name='new'),
    path('_/regularisation/wfh', views.wfh_new, name='wfh'),
    path('_/regularisation/list', views.request_list, name='list'),
    path('_/regularisation/<uuid:id>/cancel', views.cancel, name='cancel'),
    path('_/regularisation/offline', views.offline_flush, name='offline_flush'),
    path('api/v1/regularisation', views.api_requests, name='api_requests'),
    path('api/v1/regularisation/<str:id>/cancel', views.api_cancel, name='api_cancel'),
]
