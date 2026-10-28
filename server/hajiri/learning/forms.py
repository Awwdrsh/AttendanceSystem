"""
Page forms of the learning pages: they read the HTML post into the service's TS-shaped body; the rules live in
serializers.py (checked by the service), so pages and JSON bodies get the same messages.
"""
from __future__ import annotations

from django import forms

from hajiri.core.errors import BadRequestError
from hajiri.performance.forms import Flag, PostForm, _put, npt_instant, opt_text, raw_text, whole


class CourseForm(PostForm):
    code = raw_text()
    title = raw_text()
    description = opt_text()
    category = opt_text()
    delivery = opt_text()
    source = opt_text()
    provider = opt_text()
    duration_hours = opt_text()
    url = opt_text()
    cost = opt_text()
    mandatory = Flag()
    mandatory_for = opt_text()
    validity_months = opt_text()
    pass_mark = opt_text()
    active = Flag()

    def body(self) -> dict:
        d = self.cleaned()
        cost = d['cost'] or '0'
        try:
            cost = f'{float(cost):.2f}'
        except ValueError:
            pass                      # the service refuses it with the amount message
        return {'code': d['code'], 'title': d['title'], 'description': d['description'], 'category': d['category'],
                'delivery': d['delivery'] or 'classroom', 'source': d['source'] or 'internal', 'provider': d['provider'],
                'durationHours': d['duration_hours'], 'url': d['url'], 'cost': cost, 'mandatory': d['mandatory'],
                'mandatoryFor': [x.strip() for x in (d['mandatory_for'] or '').split(',') if x.strip()] if d['mandatory'] else [],
                'validityMonths': d['validity_months'], 'passMark': d['pass_mark'], 'active': d['active']}


class QuestionsForm(PostForm):
    """The assessment editor posts q<i>_prompt, repeated q<i>_option and q<i>_correct for `count` questions."""
    count = whole()

    def body(self) -> dict:
        n = self.cleaned()['count'] or 0
        questions = []
        for i in range(n):
            prompt = (self.data.get(f'q{i}_prompt') or '').strip()
            if not prompt:
                continue
            correct = forms.IntegerField(required=False, error_messages={'invalid': 'enter a whole number'})
            try:
                idx = correct.clean(self.data.get(f'q{i}_correct'))
            except forms.ValidationError:
                raise BadRequestError('enter a whole number') from None
            questions.append({'prompt': prompt, 'options': self.data.getlist(f'q{i}_option'), 'correctIndex': idx or 0})
        return {'questions': questions}


class SessionForm(PostForm):
    date = opt_text()
    start = opt_text()
    end = opt_text()
    venue = opt_text()
    trainer = opt_text()
    capacity = whole()

    def body(self) -> dict:
        d = self.cleaned()
        if not d['date']:
            raise BadRequestError('choose the date')
        return {'startsAt': npt_instant(d['date'], d['start'] or '10:00'), 'endsAt': npt_instant(d['date'], d['end'] or '13:00'),
                'venue': d['venue'], 'trainer': d['trainer'], 'capacity': d['capacity']}


class SessionStatusForm(PostForm):
    status = raw_text()


class AssignForm(PostForm):
    employee_ids = forms.Field(required=False, widget=forms.MultipleHiddenInput)
    session_id = opt_text()

    def body(self, course_id: str) -> dict:
        d = self.cleaned()
        return _put({'courseId': course_id, 'employeeIds': d['employee_ids'] or []}, 'sessionId', d['session_id'])


class JoinForm(PostForm):
    session_id = opt_text()
    reason = opt_text()

    def body(self, course_id: str) -> dict:
        d = self.cleaned()
        return _put(_put({'courseId': course_id}, 'sessionId', d['session_id']), 'reason', d['reason'])


class AttendanceForm(PostForm):
    attended = opt_text()

    def body(self) -> dict:
        return {'attended': self.cleaned()['attended'] == '1'}


class RateForm(PostForm):
    rating = whole()
    comment = opt_text()

    def body(self) -> dict:
        d = self.cleaned()
        return {'rating': d['rating'], 'comment': d['comment']}


class CertificateForm(PostForm):
    file_id = opt_text()

    def body(self) -> dict:
        file_id = self.cleaned()['file_id']
        if not file_id:
            raise BadRequestError('choose a file')
        return {'fileId': file_id}


class EnrollmentFilterForm(forms.Form):
    """The enrolment list's filter bar (GET ?course=&status=)."""
    course = opt_text()
    status = opt_text()

    def query(self) -> dict:
        self.is_valid()
        d = self.cleaned_data
        return _put(_put({}, 'courseId', d.get('course')), 'status', d.get('status'))


class AnswersForm(PostForm):
    """One chosen option per question (q0, q1, ...); every question must be answered."""

    def __init__(self, data, count: int):
        super().__init__(data)
        self.count = count
        for i in range(count):
            self.fields[f'q{i}'] = whole()

    def body(self) -> dict:
        for i in range(self.count):
            if self.data.get(f'q{i}') in (None, ''):
                raise BadRequestError(f'answer all {self.count} questions')
        d = self.cleaned()
        return {'answers': [d[f'q{i}'] for i in range(self.count)]}
