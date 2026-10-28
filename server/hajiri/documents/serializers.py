"""
The request contract of Documents & letters (the zod schemas of apps/api/src/modules/documents/routes.ts) as DRF
serializers. Field names are the TS body keys, so the JSON API, the page forms and the services speak one shape;
`hajiri.core.validation.validate` reports problems in the TS issue wording.
"""
from __future__ import annotations

from rest_framework import serializers

from hajiri.core.validation import IsoDateField
from hajiri.db.models import CompanyPolicy, EmployeeDocument, LetterRequest

CODE_RE = r'^[a-z0-9_]{2,40}$'
CODE_MESSAGE = 'lowercase letters, digits and _'
REF_PREFIX_RE = r'^[A-Za-z0-9/_-]{1,20}$'
REQUEST_FILTERS = (*LetterRequest.Status.values, 'open')


def _code():
    return serializers.RegexField(CODE_RE, error_messages={'invalid': CODE_MESSAGE})


def _text(max_length: int, **kw):
    """z.string().trim().max(n) with optional extras (no minimum, so '' is allowed)."""
    return serializers.CharField(max_length=max_length, allow_blank=True, **kw)


def _nullish_text(max_length: int, *, trim: bool = True):
    return serializers.CharField(max_length=max_length, required=False, allow_null=True, allow_blank=True,
                                 trim_whitespace=trim)


# ---------------------------------------------------------------- categories and documents

class DocumentCategoryIn(serializers.Serializer):
    code = _code()
    name = serializers.CharField(min_length=1, max_length=80)
    employeeCanUpload = serializers.BooleanField(default=True)
    expiryRequired = serializers.BooleanField(default=False)
    visibleToEmployee = serializers.BooleanField(default=True)
    sortOrder = serializers.IntegerField(min_value=0, max_value=999, default=50)


class DocumentCategoryPatchIn(serializers.Serializer):
    name = serializers.CharField(min_length=1, max_length=80, required=False)
    employeeCanUpload = serializers.BooleanField(required=False)
    expiryRequired = serializers.BooleanField(required=False)
    visibleToEmployee = serializers.BooleanField(required=False)
    sortOrder = serializers.IntegerField(min_value=0, max_value=999, required=False)
    active = serializers.BooleanField(required=False)


class DocumentIn(serializers.Serializer):
    categoryId = serializers.UUIDField()
    title = serializers.CharField(min_length=1, max_length=120)
    fileId = serializers.UUIDField()
    expiryDate = IsoDateField(required=False, allow_null=True)
    visibleToEmployee = serializers.BooleanField(required=False)
    notes = _nullish_text(500)
    replacesId = serializers.UUIDField(required=False, allow_null=True)


class DocumentPatchIn(serializers.Serializer):
    title = serializers.CharField(min_length=1, max_length=120, required=False)
    expiryDate = IsoDateField(required=False, allow_null=True)
    visibleToEmployee = serializers.BooleanField(required=False)
    notes = _nullish_text(500)


class DocumentVerifyIn(serializers.Serializer):
    decision = serializers.ChoiceField(('verify', 'reject'))
    reason = _text(300, required=False)


class DocumentHistoryQuery(serializers.Serializer):
    history = serializers.ChoiceField(('0', '1'), required=False)


class DocumentQueueQuery(serializers.Serializer):
    days = serializers.IntegerField(min_value=0, max_value=365, default=30)


# ---------------------------------------------------------------- letters

class LetterTemplateIn(serializers.Serializer):
    code = _code()
    name = serializers.CharField(min_length=1, max_length=80)
    subject = serializers.CharField(min_length=1, max_length=160)
    body = serializers.CharField(min_length=1, max_length=20000)
    requestable = serializers.BooleanField(default=False)
    visibleToEmployee = serializers.BooleanField(default=True)


class LetterTemplatePatchIn(serializers.Serializer):
    name = serializers.CharField(min_length=1, max_length=80, required=False)
    subject = serializers.CharField(min_length=1, max_length=160, required=False)
    body = serializers.CharField(min_length=1, max_length=20000, required=False)
    requestable = serializers.BooleanField(required=False)
    visibleToEmployee = serializers.BooleanField(required=False)
    active = serializers.BooleanField(required=False)


class LetterSettingsIn(serializers.Serializer):
    # z.string().trim().max(80).nullable(): the key is required, null clears it.
    signatoryName = _text(80, allow_null=True)
    signatoryTitle = _text(80, allow_null=True)
    refPrefix = serializers.RegexField(REF_PREFIX_RE, error_messages={'invalid': 'letters, digits, / _ - only'})


class LetterPreviewIn(serializers.Serializer):
    employeeId = serializers.UUIDField()
    templateId = serializers.UUIDField(required=False)
    subject = _text(160, required=False, trim_whitespace=False)
    body = _text(20000, required=False, trim_whitespace=False)
    purpose = _nullish_text(300, trim=False)
    addressedTo = _nullish_text(200, trim=False)


class LetterIssueIn(serializers.Serializer):
    templateId = serializers.UUIDField()
    employeeId = serializers.UUIDField()
    visibleToEmployee = serializers.BooleanField(required=False)
    purpose = _nullish_text(300)
    addressedTo = _nullish_text(200)
    requestId = serializers.UUIDField(required=False)
    allowMissing = serializers.BooleanField(default=False)


class LettersQuery(serializers.Serializer):
    employeeId = serializers.UUIDField(required=False)
    q = _text(80, required=False, trim_whitespace=False)
    limit = serializers.IntegerField(min_value=1, max_value=500, default=200)


class LetterVoidIn(serializers.Serializer):
    reason = serializers.CharField(min_length=3, max_length=300)


class LetterRequestIn(serializers.Serializer):
    templateId = serializers.UUIDField()
    purpose = serializers.CharField(min_length=3, max_length=300)
    addressedTo = _nullish_text(200)


class RequestsQuery(serializers.Serializer):
    status = serializers.ChoiceField(REQUEST_FILTERS, required=False)


# ---------------------------------------------------------------- policies

class CompanyPolicyIn(serializers.Serializer):
    title = serializers.CharField(min_length=1, max_length=160)
    description = _nullish_text(2000)
    fileId = serializers.UUIDField()
    audience = serializers.ChoiceField(CompanyPolicy.Audience.values, default=CompanyPolicy.Audience.ALL)
    audienceIds = serializers.ListField(child=serializers.UUIDField(), max_length=100, default=list)
    requiresAck = serializers.BooleanField(default=True)


class CompanyPolicyPatchIn(serializers.Serializer):
    title = serializers.CharField(min_length=1, max_length=160, required=False)
    description = _nullish_text(2000)
    requiresAck = serializers.BooleanField(required=False)
    archived = serializers.BooleanField(required=False)


class PolicyArchivedQuery(serializers.Serializer):
    archived = serializers.ChoiceField(('0', '1'), required=False)


# ---------------------------------------------------------------- responses (JSON API documentation)

class DocumentIdOut(serializers.Serializer):
    id = serializers.UUIDField()


class DocumentOkOut(serializers.Serializer):
    ok = serializers.BooleanField()


class DocumentCategoryOut(serializers.Serializer):
    id = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()
    employee_can_upload = serializers.BooleanField()
    expiry_required = serializers.BooleanField()
    visible_to_employee = serializers.BooleanField()
    sort_order = serializers.IntegerField()
    active = serializers.BooleanField()


class DocumentOut(serializers.Serializer):
    id = serializers.UUIDField()
    employee_id = serializers.UUIDField()
    category_id = serializers.UUIDField()
    category_name = serializers.CharField()
    title = serializers.CharField()
    version = serializers.IntegerField()
    is_current = serializers.BooleanField()
    status = serializers.ChoiceField(EmployeeDocument.Status.values)
    reject_reason = serializers.CharField(allow_null=True)
    expiry_date = serializers.CharField(allow_null=True)
    visible_to_employee = serializers.BooleanField()
    filename = serializers.CharField()
    mime = serializers.CharField()
    bytes = serializers.IntegerField()


class LetterIssuedOut(serializers.Serializer):
    id = serializers.UUIDField()
    refNo = serializers.CharField()


class PolicyRemindOut(serializers.Serializer):
    sent = serializers.IntegerField()


class LetterPreviewOut(serializers.Serializer):
    html = serializers.CharField()
    missing = serializers.ListField(child=serializers.CharField())
    unknown = serializers.ListField(child=serializers.CharField())
