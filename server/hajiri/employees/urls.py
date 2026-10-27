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
