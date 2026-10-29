"""TS /api/v1/attendance (React app, Bearer clients)."""
from django.urls import path

from . import api_views

urlpatterns = [
    path('api/v1/attendance/recompute', api_views.RecomputeView.as_view(), name='api_recompute'),
    path('api/v1/attendance/recompute-range', api_views.RecomputeRangeView.as_view(), name='api_recompute_range'),
    path('api/v1/attendance/days', api_views.DaysView.as_view(), name='api_days'),
    path('api/v1/attendance/punches', api_views.PunchesView.as_view(), name='api_punches'),
    path('api/v1/attendance/exceptions', api_views.ExceptionsView.as_view(), name='api_exceptions'),
    path('api/v1/attendance/today', api_views.TodayView.as_view(), name='api_today'),
]
