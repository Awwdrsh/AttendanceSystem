from django.urls import path

from . import api, views

urlpatterns = [
    path('kiosk', views.kiosk_page, name='page'),
    path('api/v1/kiosk/pair', api.pair, name='api_pair'),
    path('api/v1/kiosk/session', api.session, name='api_session'),
    path('api/v1/kiosk/identify', api.identify, name='api_identify'),
    path('api/v1/kiosk/punch', api.punch, name='api_punch'),
]
