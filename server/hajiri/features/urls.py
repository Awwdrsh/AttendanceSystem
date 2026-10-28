from django.urls import path

from . import api

urlpatterns = [
    path('api/v1/settings/features', api.features, name='api_features'),
]
