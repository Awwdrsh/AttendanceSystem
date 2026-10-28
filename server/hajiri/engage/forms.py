"""
Page forms of the engage hub. A form reads the HTML post (checkboxes, repeated inputs, empty selects, the survey
builder's JSON field) and gives the service its TS-shaped body; the rules themselves (lengths, ranges, choices) live
once in serializers.py and are checked by the service, so a page post and a JSON call are refused with the same
messages. A value the form cannot even read (text in a number field) is reported in the same issue shape.
"""
from __future__ import annotations

import json

from django import forms

from hajiri.core.errors import BadRequestError, ValidationError


def form_issues(form: forms.Form, names: dict[str, str] | None = None) -> ValidationError:
    """ValidationError with the form's errors as TS issues; `names` maps form fields to body keys."""
    names = names or {}
    return ValidationError([{'path': names.get(f, f) if f != '__all__' else '', 'message': str(m)}
                            for f, msgs in form.errors.items() for m in msgs])


def truthy(v) -> bool:
    """A checkbox or switch value as the pages post it ('1', 'true', 'on', 'yes')."""
    return str(v or '').lower() in ('1', 'true', 'on', 'yes')


class Text(forms.Field):
    """A text input: '' when absent, surrounding spaces removed (the service trims and checks lengths)."""

    def to_python(self, value):
        return str(value or '').strip()


class Raw(forms.Field):
    """A value exactly as posted (None when absent), for choices the service validates."""

    def to_python(self, value):
        return value


class OrNone(forms.Field):
    """An optional input or select: None when absent or blank."""

    def to_python(self, value):
        return str(value).strip() or None if value is not None else None


class Flag(forms.Field):
    """A checkbox or switch: true for '1', 'true', 'on' or 'yes', false otherwise (also when absent)."""

    def to_python(self, value):
        return truthy(value)


class Many(forms.Field):
    """Repeated inputs (several checked boxes of one name); blanks dropped."""
    widget = forms.MultipleHiddenInput

    def to_python(self, value):
        return [v for v in (value or []) if v]


class _Form(forms.Form):
    """Always bound to a post; raises instead of re-rendering (the engage pages show refusals as a toast)."""
    body_names: dict[str, str] = {}

    def __init__(self, data, **kw):
        super().__init__(data, **kw)
        for f in self.fields.values():
            f.required = False

    def cleaned(self) -> dict:
        if not self.is_valid():
            raise form_issues(self, self.body_names)
        return self.cleaned_data

    def sent(self, name: str) -> bool:
        return name in self.data


# ---------------------------------------------------------------- feed: reactions, comments, preferences

class ReactForm(_Form):
    target_type = Raw()
    target_id = Raw()
    emoji = Raw()
    compact = Flag()

    def body(self) -> dict:
        d = self.cleaned()
        return {'targetType': d['target_type'], 'targetId': d['target_id'], 'emoji': d['emoji']}


class CommentForm(_Form):
    target_type = Raw()
    target_id = Raw()
    body = Raw()

    def comment(self) -> dict:
        d = self.cleaned()
        return {'targetType': d['target_type'], 'targetId': d['target_id'], 'body': d['body'] or ''}


class PrefsForm(_Form):
    show_birthday = Flag()

    def prefs(self) -> dict:
        return {'showBirthday': self.cleaned()['show_birthday']}


# ---------------------------------------------------------------- kudos and company values

class KudosForm(_Form):
    recipient_ids = Many()
    value_id = OrNone()
    message = Text()

    def kudos(self) -> dict:
        d = self.cleaned()
        return {'recipientIds': d['recipient_ids'], 'valueId': d['value_id'], 'message': d['message']}


class ValueForm(_Form):
    name = Text()
    emoji = Text()
    description = OrNone()

    def value(self) -> dict:
        d = self.cleaned()
        return {'name': d['name'], 'emoji': d['emoji'], 'description': d['description']}


class SettingsForm(_Form):
    kudos_monthly_limit = forms.IntegerField(error_messages={'invalid': 'Expected number, received string'})
    body_names = {'kudos_monthly_limit': 'kudosMonthlyLimit'}

    def settings(self) -> dict:
        return {'kudosMonthlyLimit': self.cleaned()['kudos_monthly_limit']}


# ---------------------------------------------------------------- announcements

class AnnouncementForm(_Form):
    title = Text()
    body = Text()
    audience_kind = OrNone()
    audience_id = OrNone()
    pinned = Flag()
    requires_ack = Flag()
    expires_on = OrNone()
    popup = Flag()

    def announcement(self, *, hr: bool) -> dict:
        """HR posts to everyone by default and may make it a pop-up; a manager posts to their team."""
        d = self.cleaned()
        out = {'title': d['title'], 'body': d['body'], 'audienceKind': d['audience_kind'] or ('all' if hr else 'reports'),
               'audienceId': d['audience_id'], 'pinned': d['pinned'], 'requiresAck': d['requires_ack'],
               'expiresOn': d['expires_on']}
        if hr:
            out['popup'] = d['popup']
        return out


class PinForm(_Form):
    pinned = Flag()

    def patch(self) -> dict:
        return {'pinned': self.cleaned()['pinned']}


# ---------------------------------------------------------------- surveys

class SurveyForm(_Form):
    """The survey builder. Questions arrive as one JSON field (the builder's Alpine state)."""
    kind = OrNone()
    title = Text()
    description = OrNone()
    anonymous = Flag()
    min_group = OrNone()
    audience_kind = OrNone()
    audience_id = OrNone()
    opens_on = OrNone()
    closes_on = OrNone()
    recurrence = OrNone()
    questions_json = Raw()

    def clean_questions_json(self):
        try:
            questions = json.loads(self.cleaned_data.get('questions_json') or '[]')
        except ValueError:
            raise forms.ValidationError('Expected array, received string') from None
        if isinstance(questions, list):
            questions = [{**q, 'prompt': str(q.get('prompt', '')).strip(),
                          'options': [str(o).strip() for o in q.get('options') or [] if str(o).strip()]}
                         if isinstance(q, dict) else q for q in questions]
        return questions

    body_names = {'questions_json': 'questions'}

    def survey(self) -> dict:
        d = self.cleaned()
        min_group = d['min_group'] or '5'
        return {'kind': d['kind'] or 'survey', 'title': d['title'], 'description': d['description'],
                'anonymous': d['anonymous'], 'minGroup': int(min_group) if min_group.isdigit() else min_group,
                'audienceKind': d['audience_kind'] or 'all', 'audienceId': d['audience_id'],
                'opensOn': d['opens_on'] or '', 'closesOn': d['closes_on'] or '',
                'recurrence': d['recurrence'] or 'none', 'questions': d['questions_json']}


class ExtendForm(_Form):
    closes_on = OrNone()

    def patch(self) -> dict:
        return {'closesOn': self.cleaned()['closes_on'] or ''}


def answers_form(questions: list[dict], data) -> list[dict]:
    """The answer form's post (`q-<question id>` inputs: a score, ticked options or a written answer) as the
    service's answers. Unanswered questions are left out (the service says which required one is missing)."""
    out = []
    for q in questions:
        key = f"q-{q['id']}"
        if q['kind'] in ('rating', 'enps'):
            raw = (data.get(key) or '').strip()
            if raw:
                try:
                    out.append({'questionId': q['id'], 'value': int(raw)})
                except ValueError:
                    raise BadRequestError(f"Pick a score for: {q['prompt']}") from None
        elif q['kind'] in ('single', 'multi'):
            c = [x for x in data.getlist(key) if x]
            if c:
                out.append({'questionId': q['id'], 'choices': c})
        else:
            t = (data.get(key) or '').strip()
            if t:
                out.append({'questionId': q['id'], 'text': t})
    return out


# ---------------------------------------------------------------- suggestion box

class SuggestionForm(_Form):
    category = OrNone()
    body = Text()

    def suggestion(self) -> dict:
        d = self.cleaned()
        return {'category': d['category'] or 'general', 'body': d['body']}


class SuggestionUpdateForm(_Form):
    """Only what the post carries changes: a status button, or the share switch."""
    status = OrNone()
    is_public = Flag()

    def patch(self) -> dict:
        d = self.cleaned()
        body: dict = {}
        if d['status']:
            body['status'] = d['status']
        if self.sent('is_public'):
            body['isPublic'] = d['is_public']
        return body


class ReplyForm(_Form):
    reply = Text()
    is_public = Flag()

    def patch(self) -> dict:
        d = self.cleaned()
        return {'reply': d['reply'], 'isPublic': d['is_public']}
