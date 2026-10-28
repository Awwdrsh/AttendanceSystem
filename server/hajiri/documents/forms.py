"""
Page forms of Documents & letters. A form reads the HTML post or query string (checkboxes, optional text that posts
as '', repeated audience ids, ids that may be missing or malformed) and builds the service's TS-shaped body; the rules
themselves (lengths, choices, formats) live once in serializers.py and are checked by the service, so a page post and
a JSON call are refused with the same messages.
"""
from __future__ import annotations

import re
import uuid

from django import forms

from hajiri.core.auth import has_role
from hajiri.core.errors import BadRequestError, ValidationError


def form_issues(form: forms.Form) -> ValidationError:
    return ValidationError([{'path': f if f != '__all__' else '', 'message': str(m)}
                            for f, msgs in form.errors.items() for m in msgs])


class _Form(forms.Form):
    """Always bound to the request data; an unreadable value raises (the pages show it as a toast)."""

    def cleaned(self) -> dict:
        if not self.is_valid():
            raise form_issues(self)
        return self.cleaned_data


class OptionalUuidField(forms.CharField):
    """An id from a hidden input or the query string: anything that is not a UUID counts as not given."""

    def to_python(self, value):
        v = (super().to_python(value) or '').strip()
        try:
            return str(uuid.UUID(v)) if v else None
        except ValueError:
            return None


def _text(**kw):
    """Trimmed text; '' stays '' (the service decides whether it is allowed)."""
    return forms.CharField(required=False, **kw)


def _optional(**kw):
    """Trimmed text where blank posts as null, like `value.trim() || null` in the TS pages."""
    return forms.CharField(required=False, empty_value=None, **kw)


def _raw(**kw):
    """Text exactly as typed (template bodies keep their blank lines and indentation)."""
    return forms.CharField(required=False, strip=False, **kw)


def _flag():
    return forms.BooleanField(required=False)


# ---------------------------------------------------------------- documents

class ModeForm(_Form):
    """mode=hr&employee_id=... (HR working on an employee's documents) or mode=self."""
    mode = _text()
    employee_id = OptionalUuidField(required=False)

    def hr_mode(self, user) -> bool:
        return self.cleaned()['mode'] == 'hr' and has_role(user, 'hr')


class GridQueryForm(ModeForm):
    history = _flag()


class UploadForm(ModeForm):
    category_id = _text()
    title = _text()
    file_id = _text()
    expiry_date = _optional()
    notes = _optional()
    replaces_id = _optional()
    replaces = OptionalUuidField(required=False)
    visible_to_employee = _flag()

    def document(self, *, hr: bool, category_name: str | None) -> dict:
        d = self.cleaned()
        body = {'categoryId': d['category_id'], 'title': d['title'] or category_name or 'Document', 'fileId': d['file_id'],
                'expiryDate': d['expiry_date'], 'notes': d['notes'], 'replacesId': d['replaces_id']}
        if hr:
            body['visibleToEmployee'] = d['visible_to_employee']
        return body


class DocumentEditForm(_Form):
    title = _text()
    expiry_date = _optional()
    visible_to_employee = _flag()
    notes = _optional()

    def patch(self) -> dict:
        d = self.cleaned()
        return {'title': d['title'], 'expiryDate': d['expiry_date'], 'visibleToEmployee': d['visible_to_employee'],
                'notes': d['notes']}


class RejectForm(_Form):
    reason = _raw()
    # 'review' when sent back from HR's review list (other wording)
    from_ = _text()

    def __init__(self, data=None, *a, **kw):
        if data is not None:
            data = {'reason': data.get('reason', ''), 'from_': data.get('from', '')}
        super().__init__(data, *a, **kw)

    def from_review(self) -> bool:
        return self.cleaned()['from_'] == 'review'

    def body(self) -> dict:
        return {'decision': 'reject', 'reason': self.cleaned()['reason']}


# ---------------------------------------------------------------- document types

def slug(s: str) -> str:
    """'Work permit' -> 'work_permit' (the code HR types, made safe)."""
    return re.sub(r'^_+|_+$', '', re.sub(r'[^a-z0-9]+', '_', s.lower().strip()))[:40]


class CategoryForm(_Form):
    name = _text()
    code = _text()
    employee_can_upload = _flag()
    expiry_required = _flag()
    visible_to_employee = _flag()

    def category(self) -> dict:
        d = self.cleaned()
        return {'name': d['name'], 'code': slug(d['code']), 'employeeCanUpload': d['employee_can_upload'],
                'expiryRequired': d['expiry_required'], 'visibleToEmployee': d['visible_to_employee']}


CATEGORY_TOGGLES = {'employee_can_upload': 'employeeCanUpload', 'expiry_required': 'expiryRequired',
                    'visible_to_employee': 'visibleToEmployee', 'active': 'active'}


class CategoryToggleForm(_Form):
    key = forms.ChoiceField(choices=[(k, k) for k in CATEGORY_TOGGLES], error_messages={'invalid_choice': 'unknown setting',
                                                                                        'required': 'unknown setting'})
    value = _flag()

    def patch(self) -> dict:
        if not self.is_valid():
            raise BadRequestError('unknown setting')
        return {CATEGORY_TOGGLES[self.cleaned_data['key']]: self.cleaned_data['value']}


# ---------------------------------------------------------------- templates and letter settings

class LetterSettingsForm(_Form):
    signatory_name = _optional()
    signatory_title = _optional()
    ref_prefix = _text()

    def settings(self) -> dict:
        d = self.cleaned()
        return {'signatoryName': d['signatory_name'], 'signatoryTitle': d['signatory_title'], 'refPrefix': d['ref_prefix']}


class TemplateForm(_Form):
    code = _text()
    name = _text()
    subject = _text()
    body = _raw()
    requestable = _flag()
    visible_to_employee = _flag()
    active = _flag()

    def template(self) -> dict:
        d = self.cleaned()
        return {'code': d['code'], 'name': d['name'], 'subject': d['subject'], 'body': d['body'],
                'requestable': d['requestable'], 'visibleToEmployee': d['visible_to_employee']}

    def patch(self) -> dict:
        d = self.cleaned()
        return {'name': d['name'], 'subject': d['subject'], 'body': d['body'], 'requestable': d['requestable'],
                'visibleToEmployee': d['visible_to_employee'], 'active': d['active']}


class TemplatePreviewForm(_Form):
    preview_employee_id = OptionalUuidField(required=False)
    subject = _raw()
    body = _raw()


# ---------------------------------------------------------------- letters and requests

class LettersQueryForm(_Form):
    scope = _text()
    employee_id = OptionalUuidField(required=False)
    show_employee = _flag()
    q = _text()

    def term(self) -> str:
        return self.cleaned()['q'][:80]


class IssueForm(_Form):
    """The issue drawer and its live preview: a request fixes the employee and the template."""
    request_id = OptionalUuidField(required=False)
    template_id = _text()
    employee_id = _text()
    visible_to_employee = _flag()
    purpose = _optional()
    addressed_to = _optional()
    allow_missing = _flag()

    def ids(self, req: dict | None) -> tuple[str | None, str | None]:
        """(employee id, template id) for the preview: blanks and malformed ids count as not chosen."""
        d = self.cleaned()
        if req:
            return req['employee_id'], req['template_id']
        as_id = OptionalUuidField(required=False).clean
        return as_id(d['employee_id']), as_id(d['template_id'])

    def issue(self, req: dict | None) -> dict:
        d = self.cleaned()
        body = {'templateId': req['template_id'] if req else d['template_id'],
                'employeeId': req['employee_id'] if req else d['employee_id'],
                'visibleToEmployee': d['visible_to_employee'], 'purpose': d['purpose'], 'addressedTo': d['addressed_to'],
                'allowMissing': d['allow_missing']}
        if req:
            body['requestId'] = req['id']
        return body


class VoidForm(_Form):
    reason = _raw()


class LetterRequestForm(_Form):
    template_id = _text()
    purpose = _text()
    addressed_to = _optional()

    def letter_request(self) -> dict:
        d = self.cleaned()
        return {'templateId': d['template_id'], 'purpose': d['purpose'], 'addressedTo': d['addressed_to']}


# ---------------------------------------------------------------- policies

class PolicyForm(_Form):
    title = _text()
    description = _optional()
    file_id = _text()
    audience = _text()
    audience_ids = forms.MultipleChoiceField(required=False)
    requires_ack = _flag()

    def __init__(self, data=None, *a, **kw):
        super().__init__(data, *a, **kw)
        # Any posted id is accepted here; the service checks each one belongs to the company.
        if data is not None and hasattr(data, 'getlist'):
            self.fields['audience_ids'].choices = [(v, v) for v in data.getlist('audience_ids')]

    def policy(self) -> dict:
        d = self.cleaned()
        audience = d['audience'] or 'all'
        return {'title': d['title'], 'description': d['description'], 'fileId': d['file_id'], 'audience': audience,
                'audienceIds': [] if audience == 'all' else [x for x in d['audience_ids'] if x],
                'requiresAck': d['requires_ack']}


class ArchivedForm(_Form):
    archived = _flag()
