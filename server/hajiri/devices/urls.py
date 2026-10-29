from django.urls import path

# JSON API (the TS routes under /api/v1/devices and /api/v1/biometrics; api.py). Ids are <str:...> so a malformed
# one is the TS 400 "Invalid UUID" after the role check, not a 404.
from . import api

urlpatterns = [
    path('api/v1/devices', api.devices, name='api_devices'),
    path('api/v1/devices/punches/unmapped', api.unmapped_punches, name='api_unmapped_punches'),
    path('api/v1/devices/<str:id>/activate', api.activate, name='api_activate'),
    path('api/v1/devices/<str:id>/key', api.key, name='api_key'),
    path('api/v1/devices/<str:id>/disable', api.disable, name='api_disable'),
    path('api/v1/devices/<str:id>/mappings', api.mappings, name='api_mappings'),
    path('api/v1/devices/<str:id>/map', api.map_pin, name='api_map'),
    path('api/v1/devices/<str:id>/commands', api.commands, name='api_commands'),
    path('api/v1/devices/<str:id>/sync-users', api.sync_users, name='api_sync_users'),
    path('api/v1/biometrics/transparency', api.transparency, name='api_transparency'),
    path('api/v1/biometrics/consent', api.consent, name='api_consent'),
    path('api/v1/biometrics/consent/<str:id>/revoke', api.consent_revoke, name='api_consent_revoke'),
    path('api/v1/biometrics/templates', api.templates, name='api_templates'),
    path('api/v1/biometrics/templates/unmapped', api.templates_unmapped, name='api_templates_unmapped'),
    path('api/v1/biometrics/templates/<str:id>/shred', api.template_shred, name='api_template_shred'),
    path('api/v1/biometrics/employees/<str:id>/shred-all', api.employee_shred_all, name='api_shred_all'),
    path('api/v1/biometrics/dsar', api.dsar, name='api_dsar'),
    path('api/v1/biometrics/retention/run', api.retention_run, name='api_retention_run'),
    path('api/v1/biometrics/stats', api.stats, name='api_stats'),
]
