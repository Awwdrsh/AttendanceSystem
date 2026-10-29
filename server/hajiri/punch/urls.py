from django.urls import path

from . import api

urlpatterns = [
    # JSON (TS paths)
    path('api/v1/punch', api.punch, name='api_punch'),
    path('api/v1/punch/today', api.today, name='api_today'),
    path('api/v1/punch/remote', api.remote, name='api_remote'),
    path('api/v1/punch/photos/<str:id>', api.photo, name='photo'),
    path('api/v1/punch/manual', api.manual, name='api_manual'),
]
