from django.urls import path

from . import views

urlpatterns = [
    path('employees/onboarding', views.onboarding, name='onboarding'),
    path('employees/exits', views.exits, name='exits'),
    path('_/lifecycle/cases/new', views.case_new, name='case_new'),
    path('_/lifecycle/cases/<uuid:id>', views.case_detail, name='case'),
    path('_/lifecycle/cases/<uuid:id>/cancel', views.case_cancel, name='case_cancel'),
    path('_/lifecycle/cases/<uuid:id>/tasks', views.task_add, name='task_add'),
    path('_/lifecycle/tasks/<uuid:id>', views.task_update, name='task_update'),
    path('_/lifecycle/tasks/<uuid:id>/comments', views.comments, name='comments'),
    path('_/lifecycle/probation/<uuid:employee_id>/review', views.review, name='review'),
    path('_/lifecycle/probation/<uuid:employee_id>/confirm', views.confirm, name='confirm'),
    path('_/lifecycle/templates/<str:kind>/new', views.template_edit, name='template_new'),
    path('_/lifecycle/templates/<str:kind>/<uuid:id>', views.template_edit, name='template_edit'),
    path('_/lifecycle/assets/new', views.asset_new, name='asset_new'),
    path('_/lifecycle/assets/<uuid:id>/assign', views.asset_assign, name='asset_assign'),
    path('_/lifecycle/assets/<uuid:id>/return', views.asset_return, name='asset_return'),
    path('_/lifecycle/settings', views.settings_save, name='settings'),
    path('_/lifecycle/exits/new', views.exit_new, name='exit_new'),
    path('_/lifecycle/exits/<uuid:id>', views.exit_detail, name='exit'),
    path('_/lifecycle/exits/<uuid:id>/update', views.exit_update, name='exit_update'),
    path('_/lifecycle/exits/<uuid:id>/cancel', views.exit_cancel, name='exit_cancel'),
    path('_/lifecycle/exits/<uuid:id>/complete', views.exit_complete, name='exit_complete'),
    path('_/lifecycle/exits/<uuid:id>/withdraw', views.exit_withdraw, name='exit_withdraw'),
    path('_/lifecycle/exits/<uuid:id>/interview', views.interview, name='interview'),
    path('_/lifecycle/questions', views.questions_save, name='questions'),
    path('_/lifecycle/employees/<uuid:id>', views.employee_section, name='employee_section'),
    path('_/lifecycle/my-checklist', views.my_checklist, name='my_checklist'),
    path('_/lifecycle/my-leaving', views.my_leaving, name='my_leaving'),
    path('_/lifecycle/resign', views.resign, name='resign'),
]
