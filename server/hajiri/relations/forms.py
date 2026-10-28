"""
Page forms of employee relations. A form reads the HTML post (checkboxes, repeated inputs, empty selects, the stage
editor's numbered rows) and gives the service its TS-shaped body; the rules themselves (lengths, choices, dates) live
once in serializers.py and are checked by the service, so a page post and a JSON body are refused with the same
messages (a toast on the page).
"""
from __future__ import annotations

from django import forms

from hajiri.core.errors import BadRequestError, ValidationError

TRUE = ('1', 'true', 'on', 'yes')


def form_issues(form: forms.Form) -> ValidationError:
    return ValidationError([{'path': f if f != '__all__' else '', 'message': str(m)}
                            for f, msgs in form.errors.items() for m in msgs])


class FlagWidget(forms.CheckboxInput):
    """The last value posted for the name (a hidden '0' before a checkbox '1' reads as on)."""

    def value_from_datadict(self, data, files, name):
        return data.get(name)


class FlagField(forms.Field):
    """A checkbox or hidden flag: '1', 'true', 'on' or 'yes' is on, anything else (or nothing) is off."""
    widget = FlagWidget

    def __init__(self, **kw):
        kw.setdefault('required', False)
        super().__init__(**kw)

    def to_python(self, value) -> bool:
        return str(value or '').lower() in TRUE


class TextField(forms.CharField):
    """Text as posted (the service trims and checks it); a missing field is ''."""

    def __init__(self, **kw):
        kw.setdefault('required', False)
        kw.setdefault('strip', False)
        super().__init__(**kw)


class OptionalField(forms.CharField):
    """An optional value: blank (or only spaces) is None, the key the TS body leaves out or sends as null."""

    def __init__(self, **kw):
        kw.setdefault('required', False)
        super().__init__(**kw)

    def to_python(self, value):
        value = super().to_python(value)
        return value or None


class ListField(forms.Field):
    """Repeated inputs with the same name (checkbox groups, multi-selects, upload ids); blanks dropped."""
    widget = forms.MultipleHiddenInput

    def __init__(self, **kw):
        kw.setdefault('required', False)
        super().__init__(**kw)

    def to_python(self, value):
        return [v for v in (value or []) if v and str(v).strip()]


class _Form(forms.Form):
    """A form bound to a post that raises instead of re-rendering (relations shows refusals as a toast)."""

    def cleaned(self) -> dict:
        if not self.is_valid():
            raise form_issues(self)
        return self.cleaned_data


def _put(body: dict, key: str, value) -> dict:
    """Set an optional key only when the form had a value (the key is absent in the TS body otherwise)."""
    if value not in (None, ''):
        body[key] = value
    return body


# ---------------------------------------------------------------- grievances

class CommitteeForm(_Form):
    userIds = ListField()

    def body(self) -> dict:
        return {'userIds': self.cleaned()['userIds']}


class GrievanceForm(_Form):
    category = TextField()
    subject = TextField()
    description = TextField()
    concerns = OptionalField()
    fileIds = ListField()

    def body(self) -> dict:
        return dict(self.cleaned())


class NoteForm(_Form):
    body = TextField()
    shared = FlagField()

    def note(self) -> dict:
        d = self.cleaned()
        return {'body': d['body'], 'shared': d['shared']}


class GrievanceUpdateForm(_Form):
    """One form for the thread's three committee actions: review, hearing and exclusions (HR)."""
    action = TextField()
    date = OptionalField()
    time = OptionalField()
    place = OptionalField()
    excludeUserIds = ListField()

    def body(self) -> tuple[str, dict]:
        d = self.cleaned()
        action = d['action']
        if action == 'review':
            return action, {'status': 'under_review'}
        if action == 'hearing':
            if not d['date'] or not d['time']:
                raise BadRequestError('set the hearing date and time')
            return action, {'hearingAt': f"{d['date']}T{d['time']}:00+05:45", 'hearingPlace': d['place']}
        if action == 'exclude':
            return action, {'excludeUserIds': d['excludeUserIds']}
        raise BadRequestError('unknown action')


class GrievanceDecisionForm(_Form):
    outcome = TextField()
    findings = TextField()

    def body(self) -> dict:
        return dict(self.cleaned())


class AppealForm(_Form):
    reason = TextField()

    def body(self) -> dict:
        return dict(self.cleaned())


class AppealDecisionForm(_Form):
    outcome = TextField()
    note = TextField()
    newOutcome = OptionalField()

    def body(self) -> dict:
        d = self.cleaned()
        body = {'outcome': d['outcome'], 'note': d['note']}
        if d['outcome'] == 'decision_changed':
            _put(body, 'newOutcome', d['newOutcome'])
        return body


# ---------------------------------------------------------------- disciplinary action

class CaseForm(_Form):
    employeeId = TextField()
    category = TextField()
    incidentDate = TextField()
    incident = TextField()

    def body(self) -> dict:
        return dict(self.cleaned())


class ShowCauseForm(_Form):
    text = TextField()
    responseDue = TextField()

    def body(self) -> dict:
        return dict(self.cleaned())


class CaseResponseForm(_Form):
    response = TextField()

    def body(self) -> dict:
        return dict(self.cleaned())


class CaseDecisionForm(_Form):
    stageId = OptionalField()
    note = TextField()
    force = FlagField()
    suspensionFrom = OptionalField()
    suspensionTo = OptionalField()
    lastWorkingDay = OptionalField()

    def body(self, effect_of) -> dict:
        """`effect_of(stage_id)` gives the chosen stage's effect: the form shows the date fields only for it
        (CaseDetail), so only those are read."""
        d = self.cleaned()
        body = {'stageId': d['stageId'], 'note': d['note'], 'force': d['force']}
        effect = effect_of(d['stageId']) if d['stageId'] else None
        if effect == 'suspension':
            _put(body, 'suspensionFrom', d['suspensionFrom'])
            _put(body, 'suspensionTo', d['suspensionTo'])
        if effect == 'termination':
            _put(body, 'lastWorkingDay', d['lastWorkingDay'])
        return body


class ReasonForm(_Form):
    reason = TextField()

    def body(self) -> dict:
        return dict(self.cleaned())


STAGE_SLOTS = 20


class StagesForm(forms.Form):
    """The stage editor: up to 20 numbered rows (s-<i>-code, ...); a row counts when its s-<i>-present is posted."""

    def __init__(self, data, **kw):
        super().__init__(data, **kw)
        for i in range(STAGE_SLOTS):
            self.fields[f's-{i}-present'] = OptionalField()
            self.fields[f's-{i}-id'] = OptionalField()
            for f in ('code', 'name', 'effect', 'severity'):
                self.fields[f's-{i}-{f}'] = TextField()
            self.fields[f's-{i}-letter'] = OptionalField()
            self.fields[f's-{i}-active'] = FlagField()

    def body(self) -> dict:
        if not self.is_valid():
            raise form_issues(self)
        d = self.cleaned_data
        out = []
        for i in range(STAGE_SLOTS):
            if not d[f's-{i}-present']:
                continue
            severity = d[f's-{i}-severity'] or '1'
            try:
                severity = int(severity)
            except ValueError:
                pass  # a number the browser could not send as one is passed on, so the service reports it
            s = {'code': d[f's-{i}-code'], 'name': d[f's-{i}-name'], 'effect': d[f's-{i}-effect'],
                 'letterTemplateCode': d[f's-{i}-letter'], 'active': d[f's-{i}-active'], 'severity': severity}
            _put(s, 'id', d[f's-{i}-id'])
            out.append(s)
        return {'stages': out}


# ---------------------------------------------------------------- milestones, policy, contract

class PolicyForm(_Form):
    retirementAge = OptionalField()
    serviceYears = TextField()
    appealDays = OptionalField()

    def body(self, current: dict) -> dict:
        """The saved policy with the form's values; a number that does not read keeps the default, and service years
        are the comma or semicolon separated whole numbers from 1 to 60."""
        d = self.cleaned()

        def num(key: str, default: int) -> int:
            try:
                return int(d[key] or default)
            except ValueError:
                return default
        years = []
        for part in d['serviceYears'].replace(';', ',').split(','):
            part = part.strip()
            if part.isdigit() and 0 < int(part) <= 60:
                years.append(int(part))
        return {**current, 'retirementAge': num('retirementAge', 58), 'serviceYears': years,
                'appealDays': num('appealDays', 15)}


class DevicesPolicyForm(_Form):
    registeredDevicesOnly = FlagField()


class ContractForm(_Form):
    contractEnd = OptionalField()

    def body(self) -> dict:
        return {'contractEnd': self.cleaned()['contractEnd']}


# ---------------------------------------------------------------- acting and deputation

class AssignmentForm(_Form):
    employeeId = TextField()
    kind = TextField()
    designationId = OptionalField()
    branchId = OptionalField()
    orgUnitId = OptionalField()
    startDate = TextField()
    endDate = TextField()
    note = OptionalField()

    def body(self) -> dict:
        d = self.cleaned()
        body = {'employeeId': d['employeeId'], 'kind': d['kind'], 'startDate': d['startDate'], 'endDate': d['endDate'],
                'note': d['note']}
        # The form shows the position picker for acting and the destination pickers for deputation.
        if d['kind'] == 'acting':
            body['designationId'] = d['designationId']
        else:
            body['branchId'] = d['branchId']
            body['orgUnitId'] = d['orgUnitId']
        return body


class AssignmentEndForm(_Form):
    cancel = FlagField()

    def body(self) -> dict:
        return {'cancel': True} if self.cleaned()['cancel'] else {}


# ---------------------------------------------------------------- Know Your Employee

class CampaignForm(_Form):
    title = TextField()
    message = OptionalField()
    sections = ListField()
    audienceKind = OptionalField()
    branchId = OptionalField()
    orgUnitId = OptionalField()
    dueDate = TextField()

    def body(self) -> dict:
        d = self.cleaned()
        kind = d['audienceKind'] or 'all'
        body = {'title': d['title'], 'message': d['message'], 'sections': d['sections'], 'audienceKind': kind,
                'dueDate': d['dueDate']}
        if kind == 'branch':
            body['audienceId'] = d['branchId']
        elif kind == 'department':
            body['audienceId'] = d['orgUnitId']
        return body


class KyeResponseForm(_Form):
    status = TextField()
    note = OptionalField()

    def body(self) -> dict:
        return dict(self.cleaned())


# ---------------------------------------------------------------- devices and pop-ups

class DeviceForm(_Form):
    device_token = TextField()
    label = TextField()

    def body(self) -> dict:
        d = self.cleaned()
        return {'token': d['device_token'], 'label': d['label']}


class DismissForm(_Form):
    acknowledge = FlagField()

    def body(self) -> dict:
        return {'acknowledge': self.cleaned()['acknowledge']}
