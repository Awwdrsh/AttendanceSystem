"""
The help desk's request contract (the zod schemas of apps/api/src/modules/helpdesk/routes.ts) as DRF serializers.
Field names are the TS body keys, so JSON clients, the page forms and the services all speak the same shape;
`hajiri.core.validation.validate` turns problems into the TS issue list.
"""
from __future__ import annotations

import uuid

from rest_framework import serializers

from hajiri.db.models import HelpdeskTicket

PRIORITIES = tuple(HelpdeskTicket.Priority.values)
STATUSES = tuple(HelpdeskTicket.Status.values)
DEFAULT_SLA = {'low': 72, 'normal': 48, 'high': 24, 'urgent': 8}


def _hours():
    return serializers.IntegerField(min_value=1, max_value=720)


class SlaHoursIn(serializers.Serializer):
    low = _hours()
    normal = _hours()
    high = _hours()
    urgent = _hours()


class PolicyIn(serializers.Serializer):
    # Hours from raising a ticket to resolving it, by priority.
    slaHours = SlaHoursIn(default=lambda: dict(DEFAULT_SLA))
    # Resolved tickets close by themselves after this many days.
    autoCloseDays = serializers.IntegerField(min_value=1, max_value=60, default=7)
    # The requester can reopen a resolved ticket within this many days.
    reopenDays = serializers.IntegerField(min_value=0, max_value=60, default=7)


def _file_ids():
    return serializers.ListField(child=serializers.UUIDField(), max_length=5, default=list)


class HelpdeskCategoryIn(serializers.Serializer):
    name = serializers.CharField(min_length=1, max_length=80)
    description = serializers.CharField(max_length=300, required=False, allow_null=True, allow_blank=True,
                                        trim_whitespace=False)
    confidential = serializers.BooleanField(default=False)
    ownerUserId = serializers.UUIDField(required=False, allow_null=True)
    sort = serializers.IntegerField(min_value=0, max_value=999, default=0)
    active = serializers.BooleanField(default=True)


CategoryIn = HelpdeskCategoryIn   # the schema name differs from expenses' CategoryIn


class TicketIn(serializers.Serializer):
    categoryId = serializers.UUIDField()
    subject = serializers.CharField(min_length=3, max_length=160)
    body = serializers.CharField(min_length=1, max_length=5000)
    priority = serializers.ChoiceField(PRIORITIES, default=HelpdeskTicket.Priority.NORMAL)
    fileIds = _file_ids()


class _Assignee(serializers.CharField):
    """A user id, 'me' or 'none' (z.union([z.uuid(), z.enum(['me']), z.enum(['none'])]))."""

    def to_internal_value(self, data):
        v = super().to_internal_value(data)
        if v in ('me', 'none'):
            return v
        try:
            return str(uuid.UUID(v))
        except ValueError:
            raise serializers.ValidationError('Invalid input', code='custom') from None


class QueueQueryIn(serializers.Serializer):
    status = serializers.ChoiceField((*STATUSES, 'active'), required=False)
    categoryId = serializers.UUIDField(required=False)
    priority = serializers.ChoiceField(PRIORITIES, required=False)
    assignee = _Assignee(required=False)
    breached = serializers.ChoiceField(('1', 'true'), required=False)
    search = serializers.CharField(max_length=80, required=False, allow_blank=True, trim_whitespace=False)


class MessageIn(serializers.Serializer):
    body = serializers.CharField(min_length=1, max_length=5000)
    internal = serializers.BooleanField(default=False)
    fileIds = _file_ids()


class PatchIn(serializers.Serializer):
    status = serializers.ChoiceField(STATUSES, required=False)
    priority = serializers.ChoiceField(PRIORITIES, required=False)
    assigneeUserId = serializers.UUIDField(required=False, allow_null=True)
    categoryId = serializers.UUIDField(required=False)


class RateIn(serializers.Serializer):
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(max_length=1000, required=False, allow_null=True, allow_blank=True)


# ---------------------------------------------------------------- responses (JSON API)

class CategoryOut(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    description = serializers.CharField(allow_null=True)
    confidential = serializers.BooleanField()
    sort = serializers.IntegerField()
    active = serializers.BooleanField()
    owner_user_id = serializers.UUIDField(allow_null=True)
    owner_name = serializers.CharField(allow_null=True)


class TicketOut(serializers.Serializer):
    id = serializers.UUIDField()
    number = serializers.IntegerField()
    subject = serializers.CharField()
    status = serializers.ChoiceField(STATUSES)
    priority = serializers.ChoiceField(PRIORITIES)
    category_id = serializers.UUIDField()
    category_name = serializers.CharField()
    confidential = serializers.BooleanField()
    requester_user_id = serializers.UUIDField()
    requester_name = serializers.CharField()
    assignee_user_id = serializers.UUIDField(allow_null=True)
    assignee_name = serializers.CharField(allow_null=True)
    sla_due_at = serializers.DateTimeField()
    first_response_at = serializers.DateTimeField(allow_null=True)
    resolved_at = serializers.DateTimeField(allow_null=True)
    closed_at = serializers.DateTimeField(allow_null=True)
    rating = serializers.IntegerField(allow_null=True)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class CreatedTicketOut(serializers.Serializer):
    id = serializers.UUIDField()
    number = serializers.IntegerField()
    slaDueAt = serializers.DateTimeField()
