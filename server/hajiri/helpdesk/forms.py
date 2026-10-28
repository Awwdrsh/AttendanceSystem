"""
Page forms of the help desk. A form reads the HTML post (checkboxes, number inputs, repeated file ids, empty selects)
and gives the service its TS-shaped body (`form.body()`); the rules themselves (lengths, ranges, choices) live once in
serializers.py and are checked by the service, so a page post and a JSON call are refused with the same messages.
A value the form cannot even read (text in a number field) is reported in the same issue shape.
"""
from __future__ import annotations

from django import forms

from hajiri.core.errors import ValidationError

from .serializers import PRIORITIES


def form_issues(form: forms.Form, names: dict[str, str] | None = None) -> ValidationError:
    """ValidationError with the form's errors as TS issues; `names` maps form fields to body keys."""
    names = names or {}
    return ValidationError([{'path': names.get(f, f) if f != '__all__' else '', 'message': str(m)}
                            for f, msgs in form.errors.items() for m in msgs])


class _Form(forms.Form):
    """A form that is always bound to a post and raises instead of re-rendering (the help desk shows a toast)."""
    body_names: dict[str, str] = {}

    def cleaned(self) -> dict:
        if not self.is_valid():
            raise form_issues(self, self.body_names)
        return self.cleaned_data


class FileIdsField(forms.Field):
    """Repeated `file_ids` inputs; blanks dropped (the upload widget leaves an empty one)."""
    widget = forms.MultipleHiddenInput

    def to_python(self, value):
        return [v for v in (value or []) if v and v.strip()]


class TicketForm(_Form):
    category_id = forms.CharField(required=False)
    subject = forms.CharField(required=False, strip=False)
    body = forms.CharField(required=False, strip=False)
    priority = forms.CharField(required=False)
    file_ids = FileIdsField(required=False)

    def ticket(self) -> dict:
        d = self.cleaned()
        return {'categoryId': d['category_id'], 'subject': d['subject'], 'body': d['body'],
                'priority': d['priority'] or 'normal', 'fileIds': d['file_ids']}


class MessageForm(_Form):
    body = forms.CharField(required=False, strip=False)
    internal = forms.BooleanField(required=False)
    file_ids = FileIdsField(required=False)

    def message(self) -> dict:
        d = self.cleaned()
        return {'body': d['body'], 'internal': d['internal'], 'fileIds': d['file_ids']}


class TicketUpdateForm(_Form):
    """Only the fields the post carries are changed (status, priority, assignee, category)."""
    status = forms.CharField(required=False)
    priority = forms.CharField(required=False)
    assignee_user_id = forms.CharField(required=False)
    category_id = forms.CharField(required=False)

    def patch(self) -> dict:
        d = self.cleaned()
        sent = self.data
        body: dict = {}
        if 'status' in sent:
            body['status'] = d['status']
        if 'priority' in sent:
            body['priority'] = d['priority']
        if 'assignee_user_id' in sent:
            body['assigneeUserId'] = d['assignee_user_id'] or None
        if 'category_id' in sent:
            body['categoryId'] = d['category_id']
        return body


class RateForm(_Form):
    rating = forms.IntegerField(required=False)
    comment = forms.CharField(required=False)
    body_names = {'rating': 'rating'}

    def rating_body(self) -> dict:
        d = self.cleaned()
        return {'rating': d['rating'], **({'comment': d['comment']} if d['comment'] else {})}


class CategoryForm(_Form):
    name = forms.CharField(required=False)
    description = forms.CharField(required=False, strip=False)
    confidential = forms.BooleanField(required=False)
    owner_user_id = forms.CharField(required=False)
    sort = forms.IntegerField(required=False)
    active = forms.BooleanField(required=False)

    def category(self) -> dict:
        d = self.cleaned()
        return {'name': d['name'], 'description': d['description'] or None, 'confidential': d['confidential'],
                'ownerUserId': d['owner_user_id'] or None, 'sort': d['sort'] if d['sort'] is not None else 0,
                'active': d['active']}


class SettingsForm(_Form):
    sla_low = forms.IntegerField(required=False)
    sla_normal = forms.IntegerField(required=False)
    sla_high = forms.IntegerField(required=False)
    sla_urgent = forms.IntegerField(required=False)
    auto_close_days = forms.IntegerField(required=False)
    reopen_days = forms.IntegerField(required=False)
    body_names = {**{f'sla_{p}': f'slaHours.{p}' for p in PRIORITIES}, 'auto_close_days': 'autoCloseDays',
                  'reopen_days': 'reopenDays'}

    def policy(self) -> dict:
        d = self.cleaned()
        return {'slaHours': {p: d[f'sla_{p}'] for p in PRIORITIES}, 'autoCloseDays': d['auto_close_days'],
                'reopenDays': d['reopen_days']}


class QueueFilterForm(forms.Form):
    """The queue's filter bar (GET)."""
    status = forms.CharField(required=False)
    assignee = forms.CharField(required=False)
    category = forms.CharField(required=False)
    search = forms.CharField(required=False)
    overdue = forms.CharField(required=False)

    def filters(self) -> dict:
        self.is_valid()
        d = self.cleaned_data
        f = {'status': d.get('status') or 'active'}
        for key, name in (('assignee', 'assignee'), ('categoryId', 'category'), ('search', 'search')):
            if d.get(name):
                f[key] = d[name]
        if d.get('overdue') in ('1', 'on', 'true'):
            f['breached'] = '1'
        return f
