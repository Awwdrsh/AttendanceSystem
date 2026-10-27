from django.urls import path

from . import views

# Pages keep the React routes (hubs.tsx RelationsHub, HelpHub, ProfileHub, MyAttendanceHub); fragments live under
# /_/relations/.
urlpatterns = [
    # Employee relations hub (HR)
    path('relations', views.grievances_admin, name='grievances'),
    path('relations/disciplinary', views.disciplinary, name='disciplinary'),
    path('relations/milestones', views.milestones, name='milestones'),
    path('relations/assignments', views.assignments, name='assignments'),
    path('relations/kye', views.kye, name='kye'),
    path('relations/devices', views.devices, name='devices'),
    # Self-service
    path('me/help/grievances', views.my_grievances, name='my_grievances'),
    path('me/profile/conduct', views.conduct, name='conduct'),
    path('me/attendance/devices', views.my_devices, name='my_devices'),

    # Grievances
    path('_/relations/committee', views.committee_save, name='committee'),
    path('_/relations/grievances/new', views.grievance_new, name='grievance_new'),
    path('_/relations/grievances/<str:id>', views.grievance, name='grievance'),
    path('_/relations/grievances/<str:id>/files/<str:file_id>', views.grievance_file, name='grievance_file'),
    path('_/relations/grievances/<str:id>/notes', views.grievance_note, name='grievance_note'),
    path('_/relations/grievances/<str:id>/update', views.grievance_update, name='grievance_update'),
    path('_/relations/grievances/<str:id>/decision', views.grievance_decide, name='grievance_decide'),
    path('_/relations/grievances/<str:id>/appeal', views.grievance_appeal, name='grievance_appeal'),
    path('_/relations/grievances/<str:id>/appeal-decision', views.grievance_appeal_decide, name='grievance_appeal_decide'),
    path('_/relations/grievances/<str:id>/withdraw', views.grievance_withdraw, name='grievance_withdraw'),
    path('_/relations/grievances/<str:id>/close', views.grievance_close, name='grievance_close'),

    # Disciplinary
    path('_/relations/cases/new', views.case_new, name='case_new'),
    path('_/relations/cases/<str:id>', views.case, name='case'),
    path('_/relations/cases/<str:id>/show-cause', views.case_show_cause, name='case_show_cause'),
    path('_/relations/cases/<str:id>/decide', views.case_decide, name='case_decide'),
    path('_/relations/cases/<str:id>/withdraw', views.case_withdraw, name='case_withdraw'),
    path('_/relations/cases/<str:id>/letter', views.case_letter, name='case_letter'),
    path('_/relations/cases/<str:id>/respond', views.case_respond, name='case_respond'),
    path('_/relations/stages', views.stages, name='stages'),

    # Milestones, policy, contract
    path('_/relations/policy', views.policy, name='policy'),
    path('_/relations/employee/<str:employee_id>', views.employee_section, name='employee_section'),
    path('_/relations/employee/<str:employee_id>/contract', views.contract_save, name='contract'),

    # Acting and deputation
    path('_/relations/assignments/new', views.assignment_new, name='assignment_new'),
    path('_/relations/assignments/<str:id>/end', views.assignment_end, name='assignment_end'),

    # Know Your Employee
    path('_/relations/kye/new', views.kye_new, name='kye_new'),
    path('_/relations/kye/<str:id>', views.kye_campaign, name='kye_campaign'),
    path('_/relations/kye/<str:id>/nudge', views.kye_nudge, name='kye_nudge'),
    path('_/relations/kye/<str:id>/close', views.kye_close, name='kye_close'),
    path('_/relations/kye/<str:id>/respond', views.kye_respond, name='kye_respond'),

    # Registered devices
    path('_/relations/devices/policy', views.devices_policy, name='devices_policy'),
    path('_/relations/devices/register', views.device_register, name='device_register'),
    path('_/relations/devices/mine/<str:id>/remove', views.device_remove, name='device_remove'),
    path('_/relations/devices/<str:id>/<str:action>', views.device_decide, name='device_decide'),

    # Layout pop-ups and the Home card
    path('_/relations/popups', views.popup_notices, name='popup_notices'),
    path('_/relations/popups/<str:id>/dismiss', views.popup_dismiss, name='popup_dismiss'),
    path('_/relations/home', views.home_card, name='home_card'),
]
