from django.urls import path

# JSON API for the React app and Bearer-token clients (TS: /api/v1/employees/...). Ids are <str:...> so the service
# answers a malformed one with the TS 400 "Invalid UUID".
from . import api

urlpatterns = [
    path('api/v1/employees', api.EmployeesView.as_view(), name='api_list'),
    path('api/v1/employees/import', api.ImportView.as_view(), name='api_import'),
    path('api/v1/employees/<str:id>', api.EmployeeView.as_view(), name='api_detail'),
    path('api/v1/employees/<str:id>/sensitive', api.SensitiveView.as_view(), name='api_sensitive'),
    path('api/v1/employees/<str:id>/assignments', api.AssignmentsView.as_view(), name='api_assignments'),
    path('api/v1/employees/<str:id>/audit', api.AuditView.as_view(), name='api_audit'),
]
