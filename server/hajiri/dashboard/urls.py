from django.urls import path

from . import api

urlpatterns = [
    # JSON API (the TS routes under /api/v1/dashboard; api.py).
    path('api/v1/dashboard/executive', api.executive, name='api_executive'),
    path('api/v1/dashboard/me', api.me, name='api_me'),
]
