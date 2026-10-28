from django.urls import path

from . import views

urlpatterns = [
    # Settings -> Shifts
    path('settings/shifts', views.shift_list, name='list'),
    path('_/shifts/new', views.shift_new, name='new'),
    path('_/shifts/<uuid:id>/edit', views.shift_edit, name='edit'),
    path('_/shifts/<uuid:id>/assign', views.shift_assign, name='assign'),
    path('_/shifts/<uuid:id>/delete', views.shift_delete, name='delete'),
    # Settings -> Holidays
    path('settings/holidays', views.holidays_page, name='holidays'),
    path('_/shifts/holidays/new', views.holiday_new, name='holiday_new'),
    path('_/shifts/holidays/import', views.holiday_import, name='holiday_import'),
    path('_/shifts/holidays/<uuid:id>/edit', views.holiday_edit, name='holiday_edit'),
    path('_/shifts/holidays/<uuid:id>/delete', views.holiday_delete, name='holiday_delete'),
    # Attendance -> Rosters
    path('attendance/rosters', views.rosters, name='rosters'),
    path('_/shifts/roster', views.roster_grid, name='roster_grid'),
    path('_/shifts/roster/cell', views.roster_cell, name='roster_cell'),
    path('_/shifts/roster/copy-week', views.roster_copy_week, name='copy_week'),
    path('_/shifts/roster/publish', views.roster_publish, name='publish'),
]

# JSON API for Bearer-token clients (TS: /api/v1/shifts/...).
from rest_framework.routers import SimpleRouter  # noqa: E402

from . import api  # noqa: E402

roster_api = api.RosterViewSet.as_view({'get': 'list', 'put': 'save', 'delete': 'clear'})
urlpatterns += [
    path('api/v1/shifts/roster', roster_api, name='api-roster'),
    path('api/v1/shifts/roster/copy-week', api.RosterViewSet.as_view({'post': 'copy_week'}), name='api-roster-copy-week'),
    path('api/v1/shifts/roster/publish', api.RosterViewSet.as_view({'post': 'publish'}), name='api-roster-publish'),
]
router = SimpleRouter(trailing_slash=False)
router.register('api/v1/shifts/holidays', api.HolidayViewSet, basename='api-holiday')
router.register('api/v1/shifts/locations', api.LocationViewSet, basename='api-location')
router.register('api/v1/shifts', api.ShiftViewSet, basename='api-shift')
urlpatterns += router.urls
