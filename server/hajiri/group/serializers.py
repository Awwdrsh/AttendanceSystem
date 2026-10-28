"""Request contracts of the group JSON endpoints (DRF serializers)."""
from __future__ import annotations

from rest_framework import serializers

from engines.bs_calendar import MAX_YEAR, MIN_YEAR

from . import services


class ReportQueryIn(serializers.Serializer):
    """GET /api/v1/group/reports/<kind>?companyId=&period=month|fy&bsYear=&bsMonth=&fy="""
    companyId = serializers.UUIDField(required=False, allow_null=True)
    period = serializers.ChoiceField(('month', 'fy'), default='month')
    bsYear = serializers.IntegerField(min_value=MIN_YEAR, max_value=MAX_YEAR, required=False)
    bsMonth = serializers.IntegerField(min_value=1, max_value=12, required=False)
    fy = serializers.IntegerField(min_value=MIN_YEAR, max_value=MAX_YEAR, required=False)

    def validate(self, attrs):
        if attrs.get('period', 'month') == 'month' and ('bsYear' in attrs) != ('bsMonth' in attrs):
            raise serializers.ValidationError({'bsMonth': 'bsYear and bsMonth go together'})
        return attrs


REPORT_KINDS = tuple(services.REPORTS)
