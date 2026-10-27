"""
JSON endpoints for kiosk tablets under /api/v1/kiosk (the TS paths). The tablet has no session: after pairing it sends
its kiosk key in the X-Kiosk-Key header, so these views are CSRF-exempt (api_view) and authenticate with that key.
"""
from __future__ import annotations

from hajiri.core.http import api_view, json_response, read_json
from hajiri.web.views import client_ip

from . import services

BODY_MAX = 2_000_000


@api_view(methods=('POST',), auth=False)
def pair(request):
    return services.pair(read_json(request), client_ip(request))


def _kiosk(request) -> dict:
    return services.authenticate(request.headers.get(services.KIOSK_HEADER))


@api_view(auth=False)
def session(request):
    return services.session(_kiosk(request))


@api_view(methods=('POST',), auth=False)
def identify(request):
    k = _kiosk(request)
    return services.identify(k, read_json(request))


@api_view(methods=('POST',), auth=False)
def punch(request):
    k = _kiosk(request)
    status, body = services.punch(k, read_json(request, BODY_MAX), client_ip(request))
    return json_response(body, status=status)
