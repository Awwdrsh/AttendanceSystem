"""
The engagement request contract (the zod schemas of apps/api/src/modules/engage/routes.ts and surveys.ts) as DRF
serializers. Field names are the TS body keys, so the page forms and the services speak the same shape;
`hajiri.core.validation.validate` reports problems as the TS issue list in the zod wording.
"""
from __future__ import annotations

from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail
from rest_framework.settings import api_settings

from hajiri.core.validation import IsoDateField
from hajiri.db.models import Announcement, EngageReaction, Suggestion, Survey, SurveyQuestion

REACTIONS = tuple(EngageReaction.Emoji.values)
TARGET_TYPES = tuple(EngageReaction.TargetType.values)
SUGGESTION_CATEGORIES = tuple(Suggestion.Category.values)
SUGGESTION_STATUSES = tuple(Suggestion.Status.values)
QUESTION_KINDS = tuple(SurveyQuestion.Kind.values)


def text(min_length: int | None = None, max_length: int | None = None, *, trim: bool = True, **kw) -> serializers.CharField:
    """z.string() with .min/.max (and .trim() unless trim=False). A string without a minimum may be empty; a blank
    string under a minimum above one is reported with that minimum, as zod did."""
    if not min_length:
        kw.setdefault('allow_blank', True)
    elif min_length > 1:
        kw['error_messages'] = {'blank': f'Too small: expected string to have >={min_length} characters'}
    return serializers.CharField(min_length=min_length or None, max_length=max_length, trim_whitespace=trim, **kw)


def _uuid(**kw) -> serializers.UUIDField:
    return serializers.UUIDField(**kw)


def _nullish(field_cls, **kw):
    return field_cls(required=False, allow_null=True, **kw)


def _before():
    return serializers.DateTimeField(required=False, error_messages={'invalid': 'Invalid ISO datetime'})


def _limit():
    return serializers.IntegerField(min_value=1, max_value=50, default=20)


class StrictSerializer(serializers.Serializer):
    """z.object({...}).strict(): a key the schema does not know is refused (`Unrecognized key: "x"`)."""

    def to_internal_value(self, data):
        extra = [k for k in data if k not in self.fields] if isinstance(data, dict) else []
        try:
            value = super().to_internal_value(data)
        except serializers.ValidationError as e:
            if not extra:
                raise
            errors = dict(e.detail)
        else:
            if not extra:
                return value
            errors = {}
        errors.setdefault(api_settings.NON_FIELD_ERRORS_KEY, [])
        errors[api_settings.NON_FIELD_ERRORS_KEY] = [ErrorDetail(f'Unrecognized key: "{k}"', code='unrecognized_keys')
                                                     for k in extra] + list(errors[api_settings.NON_FIELD_ERRORS_KEY])
        raise serializers.ValidationError(errors)


# ---------------------------------------------------------------- reactions and comments

class TargetIn(serializers.Serializer):
    targetType = serializers.ChoiceField(TARGET_TYPES)
    targetId = text(1, 120, trim=False)


class ReactIn(TargetIn):
    emoji = serializers.ChoiceField(REACTIONS)


class CommentIn(TargetIn):
    body = text(1, 1000)


# ---------------------------------------------------------------- announcements

class AnnouncementIn(serializers.Serializer):
    title = text(1, 160)
    body = text(1, 8000)
    audienceKind = serializers.ChoiceField(Announcement.AudienceKind.values, default=Announcement.AudienceKind.ALL)
    audienceId = _nullish(serializers.UUIDField)
    pinned = serializers.BooleanField(default=False)
    requiresAck = serializers.BooleanField(default=False)
    expiresOn = _nullish(IsoDateField)
    # HR only: also show it as a pop-up after sign-in, until acknowledged or until this date.
    popup = serializers.BooleanField(default=False)
    popupUntil = _nullish(IsoDateField)


class AnnouncementPatchIn(StrictSerializer):
    title = text(1, 160, required=False)
    body = text(1, 8000, required=False)
    pinned = serializers.BooleanField(required=False)
    requiresAck = serializers.BooleanField(required=False)
    expiresOn = _nullish(IsoDateField)
    popup = serializers.BooleanField(required=False)
    popupUntil = _nullish(IsoDateField)


class AnnouncementQuery(serializers.Serializer):
    before = _before()
    limit = _limit()
    all = serializers.ChoiceField(('1', '0'), required=False)


# ---------------------------------------------------------------- company values and settings

class ValueIn(serializers.Serializer):
    name = text(1, 40)
    description = text(max_length=200, required=False, allow_null=True)
    emoji = text(1, 8, default='⭐')
    active = serializers.BooleanField(default=True)
    sortOrder = serializers.IntegerField(min_value=0, max_value=999, default=0)


class ValuePatchIn(serializers.Serializer):
    name = text(1, 40, required=False)
    description = text(max_length=200, required=False, allow_null=True)
    emoji = text(1, 8, required=False)
    active = serializers.BooleanField(required=False)
    sortOrder = serializers.IntegerField(min_value=0, max_value=999, required=False)


class SettingsIn(serializers.Serializer):
    kudosMonthlyLimit = serializers.IntegerField(min_value=1, max_value=100, allow_null=True)


# ---------------------------------------------------------------- kudos

class KudosIn(serializers.Serializer):
    recipientIds = serializers.ListField(child=_uuid(), min_length=1, max_length=10)
    valueId = _nullish(serializers.UUIDField)
    message = text(3, 1000)


class KudosQuery(serializers.Serializer):
    before = _before()
    limit = _limit()
    employeeId = _uuid(required=False)


class LeaderboardQuery(serializers.Serializer):
    bsYear = serializers.IntegerField(min_value=2000, max_value=2100, required=False)
    bsMonth = serializers.IntegerField(min_value=1, max_value=12, required=False)


# ---------------------------------------------------------------- feed, celebrations, preferences

class FeedQuery(serializers.Serializer):
    before = _before()
    limit = _limit()


class CelebrationsQuery(serializers.Serializer):
    days = serializers.IntegerField(min_value=1, max_value=60, default=14)


class PrefsIn(serializers.Serializer):
    showBirthday = serializers.BooleanField()


# ---------------------------------------------------------------- surveys and polls

class QuestionIn(serializers.Serializer):
    kind = serializers.ChoiceField(QUESTION_KINDS)
    prompt = text(1, 300)
    options = serializers.ListField(child=text(1, 120), max_length=12, default=list)
    required = serializers.BooleanField(default=True)


class AnswerIn(serializers.Serializer):
    questionId = _uuid()
    value = serializers.IntegerField(required=False)
    choices = serializers.ListField(child=text(max_length=120, trim=False), max_length=12, required=False)
    text = text(max_length=2000, trim=False, required=False)


class SurveyIn(serializers.Serializer):
    kind = serializers.ChoiceField(Survey.Kind.values, default=Survey.Kind.SURVEY)
    title = text(1, 160)
    description = text(max_length=2000, required=False, allow_null=True)
    anonymous = serializers.BooleanField(default=True)
    minGroup = serializers.IntegerField(min_value=3, max_value=50, default=5)
    audienceKind = serializers.ChoiceField(Survey.AudienceKind.values, default=Survey.AudienceKind.ALL)
    audienceId = _nullish(serializers.UUIDField)
    opensOn = IsoDateField()
    closesOn = IsoDateField()
    recurrence = serializers.ChoiceField(Survey.Recurrence.values, default=Survey.Recurrence.NONE)
    questions = serializers.ListField(child=QuestionIn(), min_length=1, max_length=30)


class SurveyExtendIn(StrictSerializer):
    closesOn = IsoDateField()


class RespondIn(serializers.Serializer):
    answers = serializers.ListField(child=AnswerIn(), max_length=30)


# ---------------------------------------------------------------- suggestion box

class SuggestIn(serializers.Serializer):
    category = serializers.ChoiceField(SUGGESTION_CATEGORIES, default=Suggestion.Category.GENERAL)
    body = text(5, 3000)


class SuggestionsQuery(serializers.Serializer):
    status = serializers.ChoiceField(SUGGESTION_STATUSES, required=False)


class SuggestionPatchIn(serializers.Serializer):
    status = serializers.ChoiceField(SUGGESTION_STATUSES, required=False)
    reply = text(1, 3000, required=False)
    isPublic = serializers.BooleanField(required=False)
