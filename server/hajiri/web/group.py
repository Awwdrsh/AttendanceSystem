"""
Groups of companies: GET /api/v1/group/overview, the headline figures of every company of the group the person may
see. Rules live in hajiri.core.companies; the company switcher (/api/v1/me/companies), group roles, dashboard,
reports and group administration endpoints are in hajiri.group and hajiri.group_admin.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.response import Response

from hajiri.core import companies


@extend_schema(responses=dict)
@api_view(['GET'])
def api_group_overview(request):
    """GET /api/v1/group/overview: headline figures of every company of the group I may see."""
    return Response(companies.group_overview(request.user))
