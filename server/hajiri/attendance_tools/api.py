"""JSON endpoints of the attendance tools under /api/v1/attendance-tools (the TS paths): DRF views with the project
defaults (HajiriAuthentication, IsSignedIn, the TS error shape), for Bearer-token clients. The pages call the same
service functions."""
from __future__ import annotations

from django.http import HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.response import Response

from hajiri.core.api import HasModule
from hajiri.files.parsers import json_parser

from . import serializers as s
from . import services

UPLOAD_BODY_MAX = 2_000_000          # a CSV of up to 2,000 rows inside the JSON body


@extend_schema(request=s.ExtrasPolicyIn, responses=dict)
@api_view(['GET', 'PUT'])
def policy(request):
    if request.method == 'PUT':
        return Response(services.put_policy(request.user, request.data))
    return Response(services.get_policy(request.user))


@extend_schema(parameters=[s.MonthIn], responses=dict)
@api_view(['GET'])
def penalties(request):
    return Response(services.penalties(request.user, request.query_params.dict()))


@extend_schema(parameters=[s.MonthPartialIn], responses=dict)
@api_view(['GET'])
def penalties_me(request):
    return Response(services.my_penalties(request.user, request.query_params.dict()))


@extend_schema(request=s.ApplyPenaltiesIn, responses=dict)
@api_view(['POST'])
def penalties_apply(request):
    return Response(services.apply(request.user, request.data))


@extend_schema(request=s.ReverseIn, responses=dict)
@api_view(['POST'])
def penalty_reverse(request, id):
    return Response(services.reverse(request.user, id, request.data))


@extend_schema(parameters=[s.IssueRangeIn], responses=dict)
@api_view(['GET'])
def issues(request):
    return Response(services.issues(request.user, request.query_params.dict()))


@extend_schema(parameters=[s.MonthIn], responses=dict)
@api_view(['GET'])
def issues_summary(request):
    return Response(services.issue_summary(request.user, request.query_params.dict()))


@extend_schema(request=s.EmployeeNudgeIn, responses=dict)
@api_view(['POST'])
def nudge(request):
    return Response(services.nudge(request.user, request.data))


def template_response() -> HttpResponse:
    res = HttpResponse('﻿' + services.UPLOAD_TEMPLATE, content_type='text/csv; charset=utf-8')
    res['Content-Disposition'] = 'attachment; filename="attendance-upload-template.csv"'
    return res


@extend_schema(responses={(200, 'text/csv'): str})
@api_view(['GET'])
@permission_classes([HasModule('attendance')])
def upload_template(request):
    return template_response()


@extend_schema(request=s.AttendanceUploadIn, responses=dict)
@api_view(['POST'])
@parser_classes([json_parser(UPLOAD_BODY_MAX)])
def upload_check(request):
    return Response(services.upload_check(request.user, request.data))


@extend_schema(request=s.ImportIn, responses={201: dict})
@api_view(['POST'])
@parser_classes([json_parser(UPLOAD_BODY_MAX)])
def upload_import(request):
    return Response(services.upload_import(request.user, request.data), status=201)


@extend_schema(responses=dict)
@api_view(['GET'])
def uploads(request):
    return Response(services.uploads(request.user))


@extend_schema(parameters=[s.WhosOutIn], responses=dict)
@api_view(['GET'])
def whos_out(request):
    return Response(services.whos_out_for(request.user, request.query_params.dict()))
