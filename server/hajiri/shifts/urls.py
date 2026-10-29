from django.urls import path

# JSON API for Bearer-token clients (TS: /api/v1/shifts/...).
from rest_framework.routers import SimpleRouter

from . import api

roster_api = api.RosterViewSet.as_view({'get': 'list', 'put': 'save', 'delete': 'clear'})
urlpatterns = [
    path('api/v1/shifts/roster', roster_api, name='api-roster'),
    path('api/v1/shifts/roster/copy-week', api.RosterViewSet.as_view({'post': 'copy_week'}), name='api-roster-copy-week'),
    path('api/v1/shifts/roster/publish', api.RosterViewSet.as_view({'post': 'publish'}), name='api-roster-publish'),
]
router = SimpleRouter(trailing_slash=False)
router.register('api/v1/shifts/holidays', api.HolidayViewSet, basename='api-holiday')
router.register('api/v1/shifts/locations', api.LocationViewSet, basename='api-location')
router.register('api/v1/shifts', api.ShiftViewSet, basename='api-shift')
urlpatterns += router.urls
