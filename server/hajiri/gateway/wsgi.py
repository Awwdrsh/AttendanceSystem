"""WSGI application for the gateway process: config.gateway_urls with no middleware.

Terminals are not browsers: no CSRF, sessions, security headers or HTML error pages, and they address the gateway by
IP, so any Host is accepted (the gateway is the only service on the biometric VLAN, plan §12.5)."""
from __future__ import annotations

from django.conf import settings

GATEWAY_URLCONF = 'config.gateway_urls'
GATEWAY_MIDDLEWARE: list[str] = []


def apply_gateway_settings() -> None:
    settings.ROOT_URLCONF = GATEWAY_URLCONF
    settings.MIDDLEWARE = GATEWAY_MIDDLEWARE
    settings.ALLOWED_HOSTS = ['*']
    settings.DEBUG = False
    # Bodies are read and capped by the views themselves (2 MB / 10 MB, as the TS body parsers).
    settings.DATA_UPLOAD_MAX_MEMORY_SIZE = None


def application():
    """Build the WSGI handler. Call after django.setup()."""
    from django.core.handlers.wsgi import WSGIHandler
    apply_gateway_settings()
    return WSGIHandler()
