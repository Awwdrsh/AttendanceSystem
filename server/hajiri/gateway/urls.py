"""Device gateway routes, served only by the gateway process (config.gateway_urls), never by the web app.

Paths match the TS gateway (Express): case-insensitive, with an optional trailing slash."""
from django.urls import re_path

from . import views

urlpatterns = [
    re_path(r'^(?i:health)/?$', views.health, name='health'),
    re_path(r'^(?i:public-key)/?$', views.public_key, name='public_key'),
    re_path(r'^(?i:iclock/cdata)/?$', views.cdata, name='cdata'),
    re_path(r'^(?i:iclock/getrequest)/?$', views.getrequest, name='getrequest'),
    re_path(r'^(?i:iclock/devicecmd)/?$', views.devicecmd, name='devicecmd'),
    re_path(r'^(?i:iclock/fdata)/?$', views.fdata, name='fdata'),
    re_path(r'^(?i:hikvision/event)/?$', views.hikvision_event, name='hikvision_event'),
]
