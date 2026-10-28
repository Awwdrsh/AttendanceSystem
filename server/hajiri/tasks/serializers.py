"""
The task boards' request contract (the zod schemas of apps/api/src/modules/tasks/routes.ts) as DRF serializers.
Field names are the TS body keys, so JSON clients, the page forms and the services speak the same shape;
`hajiri.core.validation.validate` turns problems into the TS issue list. The two column rules that look across the
list (unique ids, at least one done column) are checked by services.check_columns.
"""
from __future__ import annotations

from rest_framework import serializers

from hajiri.core.validation import IsoDateField
from hajiri.db.models import Task

PRIORITIES = tuple(Task.Priority.values)


class ColumnIn(serializers.Serializer):
    id = serializers.RegexField(r'^[a-z0-9-]{1,24}$', trim_whitespace=False,
                                error_messages={'invalid': 'column ids are short lowercase words'})
    name = serializers.CharField(min_length=1, max_length=40)
    done = serializers.BooleanField(default=False)


def _columns(**kw) -> serializers.ListSerializer:
    return ColumnIn(many=True, min_length=1, max_length=8, **kw)


class ColumnsIn(serializers.Serializer):
    columns = _columns()


def _user_ids(max_length: int, **kw) -> serializers.ListField:
    return serializers.ListField(child=serializers.UUIDField(), max_length=max_length, **kw)


class BoardIn(serializers.Serializer):
    name = serializers.CharField(min_length=1, max_length=80)
    memberUserIds = _user_ids(200, default=list)
    addMyReports = serializers.BooleanField(default=False)


class BoardPatchIn(serializers.Serializer):
    name = serializers.CharField(min_length=1, max_length=80, required=False)
    columns = _columns(required=False)
    moveTo = serializers.CharField(max_length=24, required=False, allow_blank=True, trim_whitespace=False)
    archived = serializers.BooleanField(required=False)


class MembersIn(serializers.Serializer):
    userIds = _user_ids(200)


class TaskIn(serializers.Serializer):
    title = serializers.CharField(min_length=1, max_length=200)
    description = serializers.CharField(max_length=5000, required=False, allow_null=True, allow_blank=True,
                                        trim_whitespace=False)
    columnId = serializers.CharField(max_length=24, required=False, allow_blank=True, trim_whitespace=False)
    priority = serializers.ChoiceField(PRIORITIES, default=Task.Priority.NORMAL)
    dueDate = IsoDateField(required=False, allow_null=True)
    labels = serializers.ListField(child=serializers.CharField(min_length=1, max_length=24), max_length=8, default=list)
    assigneeIds = _user_ids(10, default=list)


class TaskPatchIn(TaskIn):
    """taskSchema.partial() plus position; validated with partial=True, so only the keys sent come back."""
    position = serializers.FloatField(min_value=0, max_value=1e9, required=False)


class ChecklistIn(serializers.Serializer):
    text = serializers.CharField(min_length=1, max_length=300)


class ChecklistPatchIn(serializers.Serializer):
    text = serializers.CharField(min_length=1, max_length=300, required=False)
    done = serializers.BooleanField(required=False)


class CommentIn(serializers.Serializer):
    body = serializers.CharField(min_length=1, max_length=2000)


class AttachmentIn(serializers.Serializer):
    fileId = serializers.UUIDField()
