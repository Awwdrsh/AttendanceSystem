from django.urls import path

from . import api

urlpatterns = [
    # JSON for API clients and the page script (DRF views).
    path('api/v1/regularisation', api.RequestsView.as_view(), name='api_requests'),
    path('api/v1/regularisation/<str:id>/cancel', api.CancelView.as_view(), name='api_cancel'),
]
