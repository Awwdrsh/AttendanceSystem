"""JSON endpoints of the punch module under /api/v1/punch (the TS paths). The phone app and the browser clock-in
(Home) call them with a session cookie or a Bearer token."""
from __future__ import annotations

from django.http import HttpResponse

from hajiri.core.http import api_view, json_response, read_json
from hajiri.web.views import client_ip

from . import services

BODY_MAX = 2_000_000


@api_view(methods=('POST',))
def punch(request):
    status, body = services.punch(request.hajiri_user, read_json(request, BODY_MAX),
                                  device_token=request.headers.get('X-Device-Token'), ip=client_ip(request))
    return json_response(body, status=status)


@api_view()
def today(request):
    return services.today(request.hajiri_user)


@api_view()
def remote(request):
    return services.remote(request.hajiri_user, request.GET.dict())


def photo_response(p: dict) -> HttpResponse:
    res = HttpResponse(p['data'], content_type=p['mime'])
    res['Cache-Control'] = 'private, max-age=300'
    res['X-Content-Type-Options'] = 'nosniff'
    res['Content-Disposition'] = 'inline'
    return res


@api_view()
def photo(request, id):
    return photo_response(services.photo(request.hajiri_user, id))


@api_view(methods=('POST',))
def manual(request):
    status, body = services.manual(request.hajiri_user, read_json(request))
    return json_response(body, status=status)
