"""
Page forms of the approvals app. A form reads the HTML post (repeated inputs, empty selects, the step rows of a steps
editor) and gives the service its TS-shaped body; the rules themselves (lengths, ranges, choices) live once in
serializers.py and are checked by the services, so a page post and a JSON call are refused with the same messages.
"""
from __future__ import annotations

import re

from django import forms

from hajiri.core.errors import BadRequestError

from .serializers import CONDITION_KEYS


class _Form(forms.Form):
    """Always bound to a post; every field is optional text, the services validate."""

    def data_of(self) -> dict:
        self.is_valid()                       # text fields never fail; cleaned_data is filled
        return self.cleaned_data


class RepeatedField(forms.Field):
    """Repeated inputs with the same name (checkbox lists, multi-selects); blanks dropped."""
    widget = forms.MultipleHiddenInput

    def to_python(self, value):
        return [v for v in (value or []) if v and str(v).strip()]


def _number(raw: str):
    """A number input's text as a number when it is one; anything else is passed on for the serializer to refuse."""
    try:
        n = float(raw)
    except ValueError:
        return raw
    return int(n) if n.is_integer() else n


class DecisionForm(_Form):
    """Approve (and reject) buttons: an optional reason, OT minutes and the approver HR acts for."""
    reason = forms.CharField(required=False, strip=False)
    approved_min = forms.CharField(required=False)
    on_behalf_of = forms.CharField(required=False)

    def body(self) -> dict:
        d = self.data_of()
        out: dict = {}
        if d['reason']:
            out['reason'] = d['reason']
        if d['approved_min'].strip():
            out['approvedMin'] = d['approved_min'].strip()      # text; DecisionPageIn reads it as a number
        if d['on_behalf_of']:
            out['onBehalfOf'] = d['on_behalf_of']
        return out


class BulkApproveForm(_Form):
    ids = RepeatedField(required=False)

    def body(self) -> dict:
        return {'ids': self.data_of()['ids']}


class DelegateForm(_Form):
    """Delegate my approvals (DelegateApprovals.tsx)."""
    delegate_id = forms.CharField(required=False)
    from_ = forms.CharField(required=False)
    to = forms.CharField(required=False)
    note = forms.CharField(required=False)
    user_id = forms.CharField(required=False)

    def __init__(self, data=None, **kw):
        if data is not None:
            data = data.copy()
            data['from_'] = data.get('from', '')
        super().__init__(data, **kw)

    def body(self) -> dict:
        d = self.data_of()
        out = {k: v for k, v in (('delegateId', d['delegate_id']), ('from', d['from_']), ('to', d['to'])) if v}
        if d['note']:
            out['note'] = d['note']
        if d['user_id']:
            out['userId'] = d['user_id']
        return out


class GroupForm(_Form):
    name = forms.CharField(required=False)
    description = forms.CharField(required=False)
    userIds = RepeatedField(required=False)

    def body(self) -> dict:
        d = self.data_of()
        return {'name': d['name'], 'description': d['description'] or None, 'userIds': d['userIds']}


_STEP_FIELD = re.compile(r'^(?P<prefix>[a-z_]+)-(?P<i>\d+)-(?P<f>kind|role|user|group|min|label)$')


def steps_from_post(post, prefix: str) -> list[dict]:
    """The step rows of one steps editor, in order, as StepInput dicts (kind plus the field that kind needs)."""
    found: dict[int, dict] = {}
    for key in post:
        m = _STEP_FIELD.match(key)
        if m and m['prefix'] == prefix:
            found.setdefault(int(m['i']), {})[m['f']] = post.get(key)
    out = []
    for i in sorted(found):
        f = found[i]
        kind = f.get('kind') or 'manager'
        s: dict = {'kind': kind}
        if kind == 'role':
            s['role'] = f.get('role') or 'hr'
        if kind == 'user' and f.get('user'):
            s['userId'] = f['user']
        if kind == 'group' and f.get('group'):
            s['groupId'] = f['group']
        raw = (f.get('min') or '').replace(',', '').strip()
        if raw:
            n = _number(raw)
            if isinstance(n, str):
                raise BadRequestError('minAmount: Expected number, received string')
            s['minAmount'] = n
        if (f.get('label') or '').strip():
            s['label'] = f['label'].strip()   # set through the API only; the editor keeps it (toStepInput)
        out.append(s)
    return out


class FlowForm(_Form):
    """A conditional flow: name, priority, who it is for, and its steps editor (prefix 'flow')."""
    name = forms.CharField(required=False, strip=False)
    priority = forms.CharField(required=False)

    def __init__(self, data=None, **kw):
        super().__init__(data, **kw)
        for k in CONDITION_KEYS:
            self.fields[k] = RepeatedField(required=False)

    def body(self) -> dict:
        d = self.data_of()
        raw = (d['priority'] or '0').strip()
        priority = _number(raw)
        return {'name': d['name'], 'steps': steps_from_post(self.data, 'flow'),
                'conditions': {k: d[k] for k in CONDITION_KEYS}, 'priority': priority}
