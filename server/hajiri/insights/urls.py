from django.urls import path

# JSON API for Bearer-token clients (TS: /api/v1/insights/...).
from . import api

urlpatterns = [
    path('api/v1/insights', api.InsightsView.as_view(), name='api_insights'),
    path('api/v1/insights/filters', api.InsightsFiltersView.as_view(), name='api_filters'),
    path('api/v1/insights/export/<str:dataset>', api.InsightsExportView.as_view(), name='api_export'),
]
