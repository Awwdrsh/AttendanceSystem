"""
Public careers JSON (no sign-in) under /api/v1/public/careers/: the React careers site (CareersPage.jsx) and company
websites and job boards that list or post to a careers page from elsewhere (the TS routes and shapes, plus
/api/v1/public/careers/companies/<code>/jobs... naming the company). They are DRF views without authentication:
anyone may call them, no session is read and no CSRF token is needed, since nothing they do acts as a signed-in person.
"""
from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.files.parsers import json_parser
from hajiri.hiring.serializers import ApplyIn, CareersQueryIn

from . import services

APPLY_MAX_BYTES = 6 * 1024 * 1024   # a 4 MB PDF as base64 JSON (the TS API's express.json({ limit: '6mb' }))


def _ip(request) -> str:
    return request.META.get('REMOTE_ADDR') or 'unknown'


def _no_store(res):
    res['Cache-Control'] = 'no-store'
    return res


# ---------------------------------------------------------------- JSON (public)

class PublicView(APIView):
    """No authentication and no CSRF: the public careers API answers anyone the same way."""
    authentication_classes: list = []
    permission_classes = [AllowAny]


class JobListApi(PublicView):
    @extend_schema(summary='Open jobs of a careers page (?company=CODE when several companies share the install)',
                   tags=['careers'], operation_id='careers_jobs', parameters=[CareersQueryIn],
                   responses={200: OpenApiTypes.OBJECT})
    def get(self, request, code=None):
        return _no_store(Response(services.list_jobs(request.query_params.dict(), code)))


class JobApi(PublicView):
    @extend_schema(summary='One open job with its questions', tags=['careers'], operation_id='careers_job',
                   responses={200: OpenApiTypes.OBJECT})
    def get(self, request, job_id, code=None):
        return _no_store(Response(services.get_job(job_id, code)))


class ApplyApi(PublicView):
    parser_classes = [json_parser(APPLY_MAX_BYTES)]

    @extend_schema(summary='Apply for an open job (CV as a PDF data URL)', tags=['careers'], operation_id='careers_apply',
                   request=ApplyIn, responses={201: OpenApiTypes.OBJECT})
    def post(self, request, job_id, code=None):
        return Response(services.apply(job_id, request.data, _ip(request), code), status=201)


# The same endpoints under the company's code (/api/v1/public/careers/companies/<code>/jobs...).
class CompanyJobListApi(JobListApi):
    @extend_schema(summary="Open jobs of a company's careers page", tags=['careers'], operation_id='careers_company_jobs',
                   parameters=[CareersQueryIn], responses={200: OpenApiTypes.OBJECT})
    def get(self, request, code=None):
        return super().get(request, code)


class CompanyJobApi(JobApi):
    @extend_schema(summary='One open job of the company', tags=['careers'], operation_id='careers_company_job',
                   responses={200: OpenApiTypes.OBJECT})
    def get(self, request, job_id, code=None):
        return super().get(request, job_id, code)


class CompanyApplyApi(ApplyApi):
    @extend_schema(summary='Apply for an open job of the company', tags=['careers'], operation_id='careers_company_apply',
                   request=ApplyIn, responses={201: OpenApiTypes.OBJECT})
    def post(self, request, job_id, code=None):
        return super().post(request, job_id, code)
