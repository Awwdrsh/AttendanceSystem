"""
Page forms of the Growth and Performance hubs. A form reads the HTML post (checkboxes, repeated inputs, blank fields,
Nepal date and time pickers) and gives the service its TS-shaped body; the rules themselves (lengths, ranges, choices)
live once in serializers.py and are checked by the service, so a page post and a JSON body are refused with the same
messages. A value a form cannot even read (text in a whole-number field) is reported in the same issue shape.
"""
from __future__ import annotations

from django import forms

from hajiri.core.errors import BadRequestError, ValidationError


def form_issues(form: forms.Form) -> ValidationError:
    return ValidationError([{'path': f if f != '__all__' else '', 'message': str(m)} for f, msgs in form.errors.items() for m in msgs])


class PostForm(forms.Form):
    """Always bound to a post; raises instead of re-rendering (these pages show problems as a toast)."""

    def cleaned(self) -> dict:
        if not self.is_valid():
            raise form_issues(self)
        return self.cleaned_data


def opt_text():
    """A text input: trimmed, None when blank."""
    return forms.CharField(required=False, empty_value=None)


def raw_text():
    """A text input passed on as typed (the service trims and checks it)."""
    return forms.CharField(required=False, strip=False)


def whole():
    return forms.IntegerField(required=False, error_messages={'invalid': 'enter a whole number'})


class Flag(forms.Field):
    """A checkbox or a 1/0 switch: on, true, 1 and yes are True; anything else (or nothing) is False."""
    widget = forms.TextInput

    def __init__(self, **kw):
        kw.setdefault('required', False)
        super().__init__(**kw)

    def to_python(self, value):
        return value in ('on', 'true', '1', 'yes')


class Many(forms.Field):
    """Repeated inputs with the same name (pickers, tags); blanks dropped."""
    widget = forms.MultipleHiddenInput

    def __init__(self, **kw):
        kw.setdefault('required', False)
        super().__init__(**kw)

    def to_python(self, value):
        return [v for v in (value or []) if v and v.strip()]


def npt_instant(d: str | None, t: str | None) -> str | None:
    """A Nepal-time date and HH:MM as an ISO instant (parts.tsx nptInstant)."""
    if not d or not t:
        return None
    return f"{d}T{t if len(t) > 5 else t + ':00'}+05:45"


def _put(body: dict, k: str, v) -> dict:
    if v is not None:
        body[k] = v
    return body


# ---------------------------------------------------------------- goals and KPIs

class GoalForm(PostForm):
    scope = opt_text()
    title = raw_text()
    description = opt_text()
    weight = opt_text()
    due_date = opt_text()
    parent_id = opt_text()
    employee_id = opt_text()
    org_unit_id = opt_text()

    def body(self) -> dict:
        d = self.cleaned()
        scope = d['scope'] or 'individual'
        body = {'scope': scope, 'title': d['title'], 'description': d['description'], 'weight': d['weight'] or 0,
                'dueDate': d['due_date'], 'parentId': d['parent_id']}
        if scope == 'individual' and d['employee_id']:
            body['employeeId'] = d['employee_id']
        if scope == 'department':
            _put(body, 'orgUnitId', d['org_unit_id'])
        # The key result rows post their columns as parallel lists; a row without a title is an unused blank row.
        titles = self.data.getlist('kr_title')
        cols = {k: self.data.getlist(k) for k in ('kr_measure', 'kr_start', 'kr_target', 'kr_unit')}

        def at(k, i, default):
            return cols[k][i] if i < len(cols[k]) else default
        krs = []
        for i, title in enumerate(titles):
            if not title.strip():
                continue
            measure = at('kr_measure', i, 'number')
            kr = {'title': title, 'measure': measure}
            if measure != 'boolean':
                kr['startValue'] = at('kr_start', i, '0') or 0
                kr['targetValue'] = at('kr_target', i, '100') or 0
                kr['unit'] = at('kr_unit', i, '').strip() or None
            krs.append(kr)
        body['keyResults'] = krs
        return body


class CheckinForm(PostForm):
    key_result_id = opt_text()
    done = Flag()
    done_touched = opt_text()
    value = opt_text()
    progress = opt_text()
    confidence = opt_text()
    comment = opt_text()

    def body(self, goal: dict) -> dict:
        d = self.cleaned()
        body: dict = {}
        kr_id = d['key_result_id']
        if kr_id:
            body['keyResultId'] = kr_id
            kr = next((k for k in goal['key_results'] if k['id'] == kr_id), None)
            if kr and kr['measure'] == 'boolean':
                # parts.tsx sends a yes/no value only once the switch was touched; a comment alone leaves it as it is.
                if d['done_touched'] == '1':
                    body['value'] = 1 if d['done'] else 0
            elif d['value'] is not None:
                body['value'] = d['value']
        if not goal['key_results'] and d['progress'] is not None:
            body['progress'] = d['progress']
        body['confidence'] = d['confidence'] or 3
        return _put(body, 'comment', d['comment'])


class GoalStatusForm(PostForm):
    status = raw_text()


class KpiApplyForm(PostForm):
    kpi_id = opt_text()
    weight = opt_text()
    due_date = opt_text()
    employee_id = opt_text()
    target = opt_text()

    def kpi(self) -> str:
        kpi = self.cleaned()['kpi_id']
        if not kpi:
            raise BadRequestError('choose a KPI')
        return kpi

    def body(self) -> dict:
        d = self.cleaned()
        body = {'weight': d['weight'] or 0, 'dueDate': d['due_date']}
        _put(body, 'employeeId', d['employee_id'])
        return _put(body, 'target', d['target'])


class KpiForm(PostForm):
    designation = opt_text()
    title = raw_text()
    description = opt_text()
    measure = opt_text()
    default_target = opt_text()
    unit = opt_text()
    active = Flag()

    def body(self) -> dict:
        d = self.cleaned()
        return {'designation': d['designation'], 'title': d['title'], 'description': d['description'], 'measure': d['measure'] or 'number',
                'defaultTarget': d['default_target'] or 100, 'unit': d['unit'], 'active': d['active']}


# ---------------------------------------------------------------- journal and feedback

class JournalForm(PostForm):
    employee_id = raw_text()
    note_date = raw_text()
    tag = opt_text()
    note = raw_text()

    def body(self) -> dict:
        d = self.cleaned()
        return {'employeeId': d['employee_id'], 'noteDate': d['note_date'], 'tag': d['tag'] or 'neutral', 'note': d['note']}


class FeedbackForm(PostForm):
    """Give feedback, or answer a request (no recipient)."""
    to_employee_id = raw_text()
    body = raw_text()
    private = Flag()

    def give(self) -> dict:
        d = self.cleaned()
        return {'toEmployeeId': d['to_employee_id'], 'body': d['body'], 'privateToManager': d['private']}

    def answer(self) -> dict:
        d = self.cleaned()
        return {'body': d['body'], 'privateToManager': d['private']}


class FeedbackAskForm(PostForm):
    from_employee_ids = forms.Field(required=False, widget=forms.MultipleHiddenInput)
    prompt = opt_text()

    def body(self) -> dict:
        d = self.cleaned()
        return _put({'fromEmployeeIds': d['from_employee_ids'] or []}, 'prompt', d['prompt'])


# ---------------------------------------------------------------- 1-on-1s

class MeetingForm(PostForm):
    with_employee_id = raw_text()
    date = opt_text()
    time = opt_text()
    cadence = opt_text()
    agenda = raw_text()

    def body(self) -> dict:
        d = self.cleaned()
        at = npt_instant(d['date'], d['time'])
        if not at:
            raise BadRequestError('choose the date and time')
        return {'withEmployeeId': d['with_employee_id'], 'scheduledAt': at, 'cadence': d['cadence'] or 'none',
                'agenda': [x.strip() for x in (d['agenda'] or '').split('\n') if x.strip()]}


class MeetingUpdateForm(PostForm):
    action = opt_text()
    date = opt_text()
    time = opt_text()
    shared_notes = opt_text()
    private_notes = opt_text()

    def action_name(self) -> str:
        return self.cleaned()['action'] or 'notes'

    def scheduled_at(self) -> str:
        d = self.cleaned()
        at = npt_instant(d['date'], d['time'])
        if not at:
            raise BadRequestError('choose the date and time')
        return at

    def notes(self) -> dict:
        d = self.cleaned()
        return {'sharedNotes': d['shared_notes'], 'privateNotes': d['private_notes']}


class MeetingItemForm(PostForm):
    kind = opt_text()
    text = raw_text()
    owner_employee_id = opt_text()

    def body(self) -> dict:
        d = self.cleaned()
        kind = d['kind'] or 'agenda'
        return {'kind': kind, 'text': d['text'], 'ownerEmployeeId': d['owner_employee_id'] if kind == 'action' else None}


class DoneForm(PostForm):
    done = Flag()


# ---------------------------------------------------------------- reviews and peers

class ReviewForm(PostForm):
    """Ratings and comments per review item (rating_<id>, comment_<id>), the summary and, for managers, the
    recommendation."""
    summary = opt_text()
    submit = opt_text()
    increment_pct = opt_text()
    promotion = Flag()
    pip = Flag()
    training_course_ids = forms.Field(required=False, widget=forms.MultipleHiddenInput)
    notes = opt_text()

    def __init__(self, data, items: list[dict]):
        super().__init__(data)
        self.items = items
        for i in items:
            self.fields[f"rating_{i['id']}"] = whole()
            self.fields[f"comment_{i['id']}"] = opt_text()

    def body(self, manager: bool) -> dict:
        d = self.cleaned()
        body = {'items': [{'id': i['id'], 'rating': d[f"rating_{i['id']}"], 'comment': d[f"comment_{i['id']}"]} for i in self.items],
                'summary': d['summary'], 'submit': self.submitting()}
        if manager:
            body['recommendation'] = {'incrementPct': d['increment_pct'], 'promotion': d['promotion'], 'pip': d['pip'],
                                      'trainingCourseIds': d['training_course_ids'] or [], 'notes': d['notes']}
        return body

    def submitting(self) -> bool:
        return self.data.get('submit') == '1'


class CalibrateForm(PostForm):
    final_rating = raw_text()
    reason = raw_text()

    def body(self) -> dict:
        d = self.cleaned()
        return {'finalRating': d['final_rating'], 'reason': d['reason']}


class AcknowledgeForm(PostForm):
    comment = opt_text()

    def body(self) -> dict:
        return _put({}, 'comment', self.cleaned()['comment'])


class PeersForm(PostForm):
    employee_ids = forms.Field(required=False, widget=forms.MultipleHiddenInput)

    def body(self) -> dict:
        return {'employeeIds': self.cleaned()['employee_ids'] or []}


class DecideForm(PostForm):
    approve = opt_text()

    def approved(self) -> bool:
        return self.cleaned()['approve'] == '1'


class PeerResponseForm(PostForm):
    rating = whole()
    strengths = opt_text()
    improvements = opt_text()

    def body(self) -> dict:
        d = self.cleaned()
        return {'rating': d['rating'], 'strengths': d['strengths'], 'improvements': d['improvements']}


# ---------------------------------------------------------------- cycles and competencies

class CycleForm(PostForm):
    name = raw_text()
    self_due = opt_text()
    manager_due = opt_text()
    peer_due = opt_text()
    calibration_due = opt_text()
    peer_min = opt_text()
    period_start = raw_text()
    period_end = raw_text()
    goals_weight = opt_text()
    peer_enabled = Flag()
    org_unit_ids = Many()
    joined_before = opt_text()
    label_1, label_2, label_3, label_4, label_5 = raw_text(), raw_text(), raw_text(), raw_text(), raw_text()

    def body(self, draft: bool) -> dict:
        d = self.cleaned()
        deadlines = {'name': d['name'], 'selfDue': d['self_due'], 'managerDue': d['manager_due'], 'peerDue': d['peer_due'],
                     'calibrationDue': d['calibration_due'], 'peerMin': d['peer_min'] or 3}
        if not draft:
            return deadlines       # after launch only the name, deadlines and the peer minimum change
        gw = d['goals_weight'] or '0'
        try:
            cw = 100 - float(gw)
        except ValueError:
            cw = gw
        flt = {}
        if d['org_unit_ids']:
            flt['orgUnitIds'] = d['org_unit_ids']
        if d['joined_before']:
            flt['joinedBefore'] = d['joined_before']
        return {**deadlines, 'periodStart': d['period_start'], 'periodEnd': d['period_end'], 'goalsWeight': gw, 'competencyWeight': cw,
                'peerEnabled': d['peer_enabled'], 'ratingLabels': [d[f'label_{i}'] for i in range(1, 6)], 'filter': flt}


class CompetencyForm(PostForm):
    kind = opt_text()
    name = raw_text()
    description = opt_text()
    designations = forms.Field(required=False, widget=forms.MultipleHiddenInput)
    grades = forms.Field(required=False, widget=forms.MultipleHiddenInput)
    active = Flag()
    level_1, level_2, level_3, level_4, level_5 = opt_text(), opt_text(), opt_text(), opt_text(), opt_text()

    @staticmethod
    def _tags(values) -> list[str]:
        # One value per tag (tags_field.html); a comma-separated value is split too.
        return list(dict.fromkeys(x.strip() for v in values or [] for x in (v or '').split(',') if x.strip()))

    def body(self) -> dict:
        d = self.cleaned()
        return {'kind': d['kind'] or 'skill', 'name': d['name'], 'description': d['description'],
                'indicators': [{'level': n, 'text': d[f'level_{n}']} for n in range(1, 6) if d[f'level_{n}']],
                'designations': self._tags(d['designations']), 'grades': self._tags(d['grades']), 'active': d['active']}
