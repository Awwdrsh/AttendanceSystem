"""JSON endpoints of the attendance tools under /api/v1/attendance-tools (the TS paths), for Bearer-token clients and
the TS tests' shape. The pages call the same service functions."""
from __future__ import annotations

from django.http import HttpResponse

from hajiri.core.http import api_view, json_response, read_json

from . import services


@api_view(methods=('GET', 'PUT'))
def policy(request):
    if request.method == 'PUT':
        return services.put_policy(request.hajiri_user, read_json(request))
    return services.get_policy(request.hajiri_user)


@api_view()
def penalties(request):
    return services.penalties(request.hajiri_user, request.GET.dict())


@api_view()
def penalties_me(request):
    return services.my_penalties(request.hajiri_user, request.GET.dict())


@api_view(methods=('POST',))
def penalties_apply(request):
    return services.apply(request.hajiri_user, read_json(request))


@api_view(methods=('POST',))
def penalty_reverse(request, id):
    return services.reverse(request.hajiri_user, id, read_json(request))


@api_view()
def issues(request):
    return services.issues(request.hajiri_user, request.GET.dict())


@api_view()
def issues_summary(request):
    return services.issue_summary(request.hajiri_user, request.GET.dict())


@api_view(methods=('POST',))
def nudge(request):
    return services.nudge(request.hajiri_user, read_json(request))


def template_response() -> HttpResponse:
    res = HttpResponse('﻿' + services.UPLOAD_TEMPLATE, content_type='text/csv; charset=utf-8')
    res['Content-Disposition'] = 'attachment; filename="attendance-upload-template.csv"'
    return res


@api_view()
def upload_template(request):
    services._require_module(request.hajiri_user, 'attendance')
    return template_response()


@api_view(methods=('POST',))
def upload_check(request):
    return services.upload_check(request.hajiri_user, read_json(request, 2_000_000))


@api_view(methods=('POST',))
def upload_import(request):
    return json_response(services.upload_import(request.hajiri_user, read_json(request, 2_000_000)), status=201)


@api_view()
def uploads(request):
    return services.uploads(request.hajiri_user)


@api_view()
def whos_out(request):
    return services.whos_out_for(request.hajiri_user, request.GET.dict())
