"""
Page forms of the hiring hub: they read the HTML side of a post (number inputs with thousands separators, '1'
checkboxes, repeated panel inputs, the job editor's JSON fields) into the TS-shaped body the services validate with
their serializers. The fields here never refuse a value: a value they cannot read is passed on as typed, so the
serializer reports it with the same message a JSON client gets.
"""
from __future__ import annotations

import json

from django import forms

from . import labels as L


def _number(v: str | None):
    """'1,500' -> 1500, '2.5' -> 2.5, '' -> None, anything else as typed (the serializer reports it)."""
    v = (v or '').strip().replace(',', '')
    if v == '':
        return None
    try:
        f = float(v)
    except ValueError:
        return v
    return int(f) if f.is_integer() else f


def _money(v: str | None):
    """An amount as text: '50,000' -> '50000', '' -> None, anything else as typed."""
    v = (v or '').strip().replace(',', '')
    if v == '':
        return None
    try:
        f = float(v)
    except ValueError:
        return v
    return str(int(f)) if f.is_integer() else v


class NumberInput(forms.Field):
    def __init__(self, *, default=None, **kw):
        self.fallback = default
        super().__init__(required=False, **kw)

    def to_python(self, value):
        n = _number(value)
        return n if n not in (None, 0) or self.fallback is None else self.fallback


class MoneyInput(forms.Field):
    def __init__(self, **kw):
        super().__init__(required=False, **kw)

    def to_python(self, value):
        return _money(value)


class OptionalText(forms.CharField):
    """Trimmed text; empty is None."""

    def __init__(self, **kw):
        super().__init__(required=False, empty_value=None, **kw)


class RawText(forms.CharField):
    """Text as typed (the serializer trims and checks it)."""

    def __init__(self, **kw):
        super().__init__(required=False, strip=False, **kw)


class Flag(forms.Field):
    """A checkbox or hidden input that posts '1' when on."""

    def __init__(self, **kw):
        super().__init__(required=False, **kw)

    def to_python(self, value):
        return value == '1'


class JsonInput(forms.Field):
    """A hidden input holding JSON written by the page's editor; unreadable JSON is the default."""

    def __init__(self, default, **kw):
        self.fallback = default
        super().__init__(required=False, **kw)

    def to_python(self, value):
        try:
            return json.loads(value) if value else self.fallback
        except ValueError:
            return self.fallback


class BodyForm(forms.Form):
    """A form whose fields always clean; to_body() is the service input."""

    def data_dict(self) -> dict:
        self.is_valid()
        return self.cleaned_data

    def to_body(self) -> dict:  # pragma: no cover - every form defines it
        raise NotImplementedError


# ---------------------------------------------------------------- jobs

def editor_questions(qs) -> list:
    """The editor's questions with only the rule that is chosen (setRule in JobForm.tsx)."""
    out = []
    for q in qs if isinstance(qs, list) else []:
        if not isinstance(q, dict):
            continue
        item = {k: q[k] for k in ('id', 'kind', 'label', 'required') if k in q}
        rule = q.get('rule')
        if rule in ('knockout', 'preferred') and q.get('kind') != 'text':
            item[rule] = ({'min': _number(str(q.get('min') if q.get('min') is not None else '')) or 0} if q.get('kind') == 'number'
                          else {'equals': q.get('equals') or 'yes'})
        elif rule is None:
            for k in ('knockout', 'preferred'):
                if q.get(k) is not None:
                    item[k] = q[k]
        out.append(item)
    return out


class JobForm(BodyForm):
    title = RawText()
    category = OptionalText()
    location = OptionalText()
    workMode = forms.CharField(required=False)
    employmentType = forms.CharField(required=False)
    openings = NumberInput(default=1)
    experienceMin = NumberInput()
    experienceMax = NumberInput()
    salaryMin = MoneyInput()
    salaryMax = MoneyInput()
    showSalary = Flag()
    deadline = OptionalText()
    orgUnitId = OptionalText()
    branchId = OptionalText()
    hiringManagerId = OptionalText()
    description = RawText()
    requirements = RawText()
    stages_json = JsonInput([])
    questions_json = JsonInput([])

    def to_body(self) -> dict:
        d = self.data_dict()
        out = {k: d[k] for k in ('title', 'category', 'location', 'openings', 'experienceMin', 'experienceMax', 'salaryMin',
                                 'salaryMax', 'showSalary', 'deadline', 'orgUnitId', 'branchId', 'hiringManagerId',
                                 'description', 'requirements')}
        out['workMode'] = d['workMode'] or 'onsite'
        out['employmentType'] = d['employmentType'] or 'full_time'
        stages = d['stages_json'] if isinstance(d['stages_json'], list) else []
        out['stages'] = [s for s in stages if isinstance(s, str)]
        out['questions'] = editor_questions(d['questions_json'])
        return out


class PolicyForm(BodyForm):
    retentionDays = NumberInput(default=180)
    careersEnabled = Flag()

    def to_body(self) -> dict:
        return dict(self.data_dict())


# ---------------------------------------------------------------- requisitions

class RequisitionForm(BodyForm):
    title = RawText()
    designation = OptionalText()
    orgUnitId = OptionalText()
    branchId = OptionalText()
    headcount = NumberInput(default=1)
    salaryMin = MoneyInput()
    salaryMax = MoneyInput()
    kind = forms.CharField(required=False)
    replacingEmployeeId = OptionalText()
    justification = RawText()

    def to_body(self) -> dict:
        d = dict(self.data_dict())
        d['kind'] = d['kind'] or 'new'
        if d['kind'] != 'replacement':
            d['replacingEmployeeId'] = None
        return d


# ---------------------------------------------------------------- applications

class CandidateForm(BodyForm):
    name = RawText()
    email = forms.CharField(required=False)
    phone = OptionalText()
    source = forms.CharField(required=False)
    experienceYears = NumberInput()
    currentEmployer = OptionalText()
    coverNote = OptionalText()
    cvFileId = OptionalText()

    def to_body(self) -> dict:
        d = dict(self.data_dict())
        d['source'] = d['source'] or 'referral'
        return d


class ApplicationForm(BodyForm):
    """Rating, tags and owner: only the inputs that were posted change."""
    rating = NumberInput()
    tags = forms.CharField(required=False)
    ownerUserId = OptionalText()

    def to_body(self) -> dict:
        d = self.data_dict()
        out: dict = {}
        if 'rating' in self.data:
            out['rating'] = d['rating'] or None
        if 'tags' in self.data:
            out['tags'] = [t.strip() for t in d['tags'].split(',') if t.strip()]
        if 'ownerUserId' in self.data:
            out['ownerUserId'] = d['ownerUserId']
        return out


class RejectForm(BodyForm):
    reason = RawText()
    notifyCandidate = Flag()

    def to_body(self) -> dict:
        return dict(self.data_dict())


class NoteForm(BodyForm):
    body = RawText()

    def to_body(self) -> dict:
        return dict(self.data_dict())


# ---------------------------------------------------------------- interviews, offers, hire

class InterviewForm(BodyForm):
    title = forms.CharField(required=False, strip=False)
    date = RawText()
    time = RawText()
    durationMin = NumberInput(default=45)
    mode = RawText()
    location = OptionalText()
    panel = forms.MultipleChoiceField(required=False)
    notifyCandidate = Flag()

    def __init__(self, data=None, **kw):
        super().__init__(data, **kw)
        # The panel's ids are checked by the service against the people of the company.
        self.fields['panel'].valid_value = lambda value: True

    def to_body(self) -> dict:
        d = dict(self.data_dict())
        d['title'] = d['title'] or 'Interview'
        return d


class ScorecardForm(BodyForm):
    """Stars per criterion (rating_0 ... rating_n; 0 or nothing is 'not rated'), a recommendation and notes."""
    recommendation = RawText()
    notes = OptionalText()

    def to_body(self) -> dict:
        d = self.data_dict()
        ratings = {}
        for i, c in enumerate(L.DEFAULT_CRITERIA):
            raw = (self.data.get(f'rating_{i}') or '0')
            if raw.isdigit() and int(raw) > 0:
                ratings[c] = int(raw)
        return {'ratings': ratings, 'recommendation': d['recommendation'], 'notes': d['notes']}


class OfferForm(BodyForm):
    designation = RawText()
    salary = MoneyInput()
    joinDate = RawText()
    expiresOn = OptionalText()
    notes = OptionalText()

    def to_body(self) -> dict:
        d = dict(self.data_dict())
        d['salary'] = d['salary'] or ''
        return d


class HireForm(BodyForm):
    empCode = forms.CharField(required=False)
    probationEnd = OptionalText()
    gender = OptionalText()
    shiftId = OptionalText()

    def to_body(self) -> dict:
        d = self.data_dict()
        out = {'empCode': d['empCode'].upper(), 'probationEnd': d['probationEnd']}
        if d['gender']:
            out['gender'] = d['gender']
        if d['shiftId']:
            out['shiftId'] = d['shiftId']
        return out


class RespondForm(BodyForm):
    response = RawText()

    def to_body(self) -> dict:
        return dict(self.data_dict())


class InterviewStatusForm(BodyForm):
    status = OptionalText()

    def to_body(self) -> dict:
        s = self.data_dict()['status']
        return {'status': s} if s else {}
