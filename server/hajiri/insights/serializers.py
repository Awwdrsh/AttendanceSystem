"""
Request contract of the Insights endpoints (the TS route's zod `filterQuery`): BS months as YYYY-MM plus an optional
branch and department. Used by the service for pages and the JSON API alike.
"""
from __future__ import annotations

from rest_framework import serializers

MONTH = r'^\d{4}-\d{1,2}$'


class InsightsFilterIn(serializers.Serializer):
    """?from=&to=&branchId=&orgUnitId= ('from' is a Python keyword, so the fields are built in get_fields)."""

    def get_fields(self):
        return {
            'from': serializers.RegexField(MONTH, required=False),
            'to': serializers.RegexField(MONTH, required=False),
            'branchId': serializers.UUIDField(required=False),
            'orgUnitId': serializers.UUIDField(required=False),
        }


class FilterOptionOut(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class OrgUnitOptionOut(FilterOptionOut):
    kind = serializers.CharField()
    parentId = serializers.UUIDField(allow_null=True)


class FilterOptionsOut(serializers.Serializer):
    branches = FilterOptionOut(many=True)
    orgUnits = OrgUnitOptionOut(many=True)
