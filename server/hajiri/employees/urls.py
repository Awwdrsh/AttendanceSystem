from django.urls import path

from . import views

urlpatterns = [
    path('employees', views.employee_list, name='list'),
    path('employees/<uuid:id>', views.employee_detail, name='detail'),
    path('_/employees/new', views.employee_new, name='new'),
    path('_/employees/import', views.employee_import, name='import'),
    path('_/employees/<uuid:id>/edit', views.employee_edit, name='edit'),
    path('_/employees/<uuid:id>/overview', views.overview, name='overview'),
    path('_/employees/<uuid:id>/sensitive', views.sensitive, name='sensitive'),
]

# JSON API for Bearer-token clients (TS: /api/v1/employees/...).
from rest_framework.routers import SimpleRouter  # noqa: E402

from . import api  # noqa: E402

router = SimpleRouter(trailing_slash=False)
router.register('api/v1/employees', api.EmployeeViewSet, basename='api-employee')
urlpatterns += router.urls
