from django.urls import path

from . import views

urlpatterns = [
    path('settings/devices', views.device_list, name='list'),
    path('_/devices/map', views.device_map, name='map'),
    path('_/devices/<uuid:id>', views.device_detail, name='detail'),
    path('_/devices/<uuid:id>/commands', views.device_commands, name='commands'),
    path('_/devices/<uuid:id>/mappings', views.device_mappings, name='mappings'),
    path('_/devices/<uuid:id>/activate', views.device_activate, name='activate'),
    path('_/devices/<uuid:id>/disable', views.device_disable, name='disable'),
    path('_/devices/<uuid:id>/key', views.device_key, name='key'),
    path('_/devices/<uuid:id>/sync-users', views.device_sync, name='sync_users'),
    path('_/devices/<uuid:id>/command', views.device_command, name='command'),
    # Biometrics: actions and fragments for My data (/me/profile/privacy) and the employee detail page.
    path('_/devices/biometrics/consent', views.consent_grant, name='consent'),
    path('_/devices/biometrics/consent/<uuid:id>/revoke', views.consent_revoke, name='consent_revoke'),
    path('_/devices/biometrics/consents', views.consents_fragment, name='consents'),
    path('_/devices/biometrics/templates', views.templates_fragment, name='templates'),
    path('_/devices/biometrics/templates/<uuid:id>/shred', views.template_shred, name='template_shred'),
    path('_/devices/biometrics/employees/<uuid:id>/shred-all', views.employee_shred_all, name='shred_all'),
    path('_/devices/biometrics/transparency', views.transparency_fragment, name='transparency'),
    path('_/devices/biometrics/dsar', views.dsar_download, name='dsar'),
    path('_/devices/biometrics/retention/run', views.retention_run, name='retention_run'),
]
