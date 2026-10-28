"""
Page forms of time tracking. Each form reads an HTML post (checkboxes posted as 'true'/'on', number inputs, repeated
member_ids, empty selects) and builds the service's TS-shaped body; the rules themselves (lengths, ranges, choices)
live once in serializers.py and are checked by the service, so a page post and a JSON call are refused with the same
messages. A number field holding text is passed on as text, so the service reports it in the zod wording.
"""
from __future__ import annotations

from django import forms

from hajiri.core.errors import ValidationError

BRAND = '#12773a'
TRUE = ('1', 'true', 'on', 'yes')


def form_issues(form: forms.Form) -> ValidationError:
    return ValidationError([{'path': f if f != '__all__' else '', 'message': str(m)}
                            for f, msgs in form.errors.items() for m in msgs])


class FlagField(forms.Field):
    """A checkbox or a 'true'/'false' select: True, False, or None when the field was not posted (or left blank)."""
    widget = forms.HiddenInput

    def to_python(self, value):
        if value in (None, ''):
            return None
        return str(value).lower() in TRUE


class NumberField(forms.Field):
    """A number input: an int when whole, a float otherwise, None when blank; text that is no number is kept so the
    service answers it with the serializer's message."""

    def to_python(self, value):
        value = value.strip() if isinstance(value, str) else value
        if value in (None, ''):
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return value
        return int(f) if f.is_integer() else f


class OptionalId(forms.CharField):
    """A select of ids where '' means none."""

    def __init__(self, **kw):
        super().__init__(required=False, **kw)

    def to_python(self, value):
        value = super().to_python(value)
        return value or None


class IdsField(forms.Field):
    """Repeated inputs (a checklist); blanks dropped."""
    widget = forms.MultipleHiddenInput

    def to_python(self, value):
        return [v for v in (value or []) if v and v.strip()]


class _Form(forms.Form):
    """Always bound to a post; raises instead of re-rendering (time pages show errors as toasts)."""

    def cleaned(self) -> dict:
        if not self.is_valid():
            raise form_issues(self)
        return self.cleaned_data


def _note(d: dict) -> str | None:
    return d.get('note') or None


class TimerForm(_Form):
    """TimerCard's Start (project, activity, note) and Stop (note)."""
    project_id = OptionalId()
    activity_id = OptionalId()
    note = forms.CharField(required=False)

    def start(self) -> dict:
        d = self.cleaned()
        return {'projectId': d['project_id'], 'activityId': d['activity_id'], 'note': _note(d)}

    def stop(self) -> dict:
        return {'note': _note(self.cleaned())}


class EntryForm(_Form):
    """EntryModal: a start and end time, or a duration. A running timer only changes what it is for."""
    date = forms.CharField(required=False, strip=False)
    mode = forms.CharField(required=False)
    start = forms.CharField(required=False, strip=False)
    end = forms.CharField(required=False, strip=False)
    duration_min = NumberField(required=False)
    project_id = OptionalId()
    activity_id = OptionalId()
    note = forms.CharField(required=False)
    billable = FlagField(required=False)

    def body(self, running: bool) -> dict:
        d = self.cleaned()
        body: dict = {'projectId': d['project_id'], 'activityId': d['activity_id'], 'note': _note(d)}
        if running:
            return body
        body['date'] = d['date']
        if d['billable'] is not None:
            body['billable'] = d['billable']
        if (d['mode'] or 'range') == 'range':
            body['start'] = d['start']
            body['end'] = d['end']
        elif d['duration_min'] is not None:
            body['durationMin'] = d['duration_min']
        return body


class NoteForm(_Form):
    """Submit the week (with an optional note to the approver)."""
    week = forms.CharField(required=False)
    note = forms.CharField(required=False)

    def note_value(self) -> str | None:
        return _note(self.cleaned())


class DecisionForm(_Form):
    decision = forms.CharField(required=False)
    reason = forms.CharField(required=False)

    def decision_value(self) -> tuple[str, str]:
        d = self.cleaned()
        return d['decision'], d['reason']


class ProjectForm(_Form):
    """The project modal: code, name, client, colour swatch, billing, budget and who may log to it."""
    code = forms.CharField(required=False)
    name = forms.CharField(required=False, strip=False)
    client_id = OptionalId()
    color = forms.CharField(required=False, strip=False)
    billable = FlagField(required=False)
    bill_rate = NumberField(required=False)
    budget_hours = NumberField(required=False)
    all_members = FlagField(required=False)
    member_ids = IdsField(required=False)          # the service checks each one is an employee of the company
    archived = FlagField(required=False)

    def body(self, editing: bool) -> dict:
        d = self.cleaned()
        all_members = bool(d['all_members'])
        return {
            'code': d['code'].upper(), 'name': d['name'], 'clientId': d['client_id'], 'color': d['color'] or BRAND,
            'billable': bool(d['billable']), 'billRate': d['bill_rate'], 'budgetHours': d['budget_hours'],
            'allMembers': all_members, 'memberIds': [] if all_members else d['member_ids'],
            'status': 'archived' if editing and d['archived'] else 'active',
        }


class NameForm(_Form):
    """Quick add of a client or an activity (an activity may belong to one project)."""
    name = forms.CharField(required=False)
    project_id = OptionalId()

    def client(self) -> dict:
        return {'name': self.cleaned()['name']}

    def activity(self) -> dict:
        d = self.cleaned()
        return {'name': d['name'], 'projectId': d['project_id']}


class ArchiveForm(_Form):
    archived = FlagField(required=False)

    def body(self) -> dict:
        return {'archived': bool(self.cleaned()['archived'])}


class SettingsForm(_Form):
    """The Time tracking card of Settings (TimeSettings.tsx)."""
    enabled = FlagField(required=False)
    require_project = FlagField(required=False)
    rounding_step = NumberField(required=False)
    rounding_mode = forms.CharField(required=False)
    # The direction select is disabled (and not posted) while rounding is off; a hidden input keeps its value.
    rounding_mode_kept = forms.CharField(required=False)
    friday_reminder = FlagField(required=False)
    auto_stop_hours = NumberField(required=False)

    def policy(self) -> dict:
        d = self.cleaned()
        hours = d['auto_stop_hours']
        # NumberInput clamps to 1-24 and falls back to 12.
        if not isinstance(hours, (int, float)) or not hours:
            hours = 12
        return {'enabled': bool(d['enabled']), 'requireProject': bool(d['require_project']),
                'roundingStep': d['rounding_step'] or 0,
                'roundingMode': d['rounding_mode'] or d['rounding_mode_kept'] or 'nearest',
                'fridayReminder': bool(d['friday_reminder']), 'autoStopHours': int(min(24, max(1, hours)))}
