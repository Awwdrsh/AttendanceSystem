"""
Page forms of the lifecycle pages. Each form reads the HTML post (empty inputs, checkboxes, repeated rows) into the TS
body the route function validates with serializers.py, so the pages and any JSON caller are refused with the same
messages.
"""
from __future__ import annotations

from hajiri.core.errors import ValidationError
from hajiri.people.forms import Check, PostForm, Text, blank


def _b(v):
    """Blank is None; text is stripped."""
    v = blank(v)
    return v.strip() if isinstance(v, str) else v


def _rows(post, prefix: str) -> list[int]:
    return sorted({int(k.split('-')[1]) for k in post if k.startswith(prefix + '-') and k.split('-')[1].isdigit()})


class StartCaseForm(PostForm):
    employee_id = Text()
    templateId = Text()
    anchorDate = Text()

    def body(self) -> dict:
        d = self.cleaned()
        body = {'employeeId': d['employee_id'] or '', 'templateId': _b(d['templateId'])}
        if _b(d['anchorDate']):
            body['anchorDate'] = d['anchorDate']
        return body


class AddTaskForm(PostForm):
    title = Text()
    assigneeType = Text()
    dueDate = Text()

    def body(self) -> dict:
        d = self.cleaned()
        return {'title': (d['title'] or '').strip(), 'assigneeType': d['assigneeType'] or 'hr', 'dueDate': _b(d['dueDate'])}


class TaskStatusForm(PostForm):
    status = Text()

    def body(self) -> dict:
        status = self.cleaned()['status']
        if status not in ('open', 'done', 'skipped'):
            raise ValidationError([{'path': 'status', 'message': 'Invalid option: expected one of "open"|"done"|"skipped"'}])
        return {'status': status}


class CommentForm(PostForm):
    body = Text()

    def comment(self) -> dict:
        return {'body': self.cleaned()['body'] or ''}


class ReviewForm(PostForm):
    recommendation = Text()
    rating = Text()
    comments = Text()
    extendTo = Text()
    lastWorkingDay = Text()

    def body(self, employee_id: str) -> dict:
        d = self.cleaned()
        rec = d['recommendation'] or 'confirm'
        try:
            rating = int(d['rating'] or 4)
        except ValueError:
            rating = d['rating']
        return {'employeeId': str(employee_id), 'rating': rating, 'recommendation': rec, 'comments': (d['comments'] or '').strip(),
                'extendTo': _b(d['extendTo']) if rec == 'extend' else None,
                'lastWorkingDay': _b(d['lastWorkingDay']) if rec == 'terminate' else None}


class ConfirmForm(PostForm):
    confirmedOn = Text()

    def body(self, employee_id: str) -> dict:
        return {'employeeId': str(employee_id), 'confirmedOn': self.cleaned()['confirmedOn'] or ''}


class TemplateForm(PostForm):
    """The checklist editor: tasks posted as t-<i>-<field>; blank titles are dropped; t-<i>-sort (move up / down)
    orders them, otherwise slot order."""
    name = Text()
    description = Text()
    isDefault = Check()
    active = Check()

    def body(self, kind: str) -> dict:
        d = self.cleaned()
        p = self.data
        idx = _rows(p, 't')

        def position(i: int):
            v = p.get(f't-{i}-sort') or ''
            return (int(v), i) if v.lstrip('-').isdigit() else (i, i)
        idx.sort(key=position)
        tasks = []
        for i in idx:
            title = (p.get(f't-{i}-title') or '').strip()
            if not title:
                continue
            at = p.get(f't-{i}-assigneeType') or 'hr'
            try:
                off = int(p.get(f't-{i}-dueOffsetDays') or 0)
            except ValueError:
                off = 0
            tasks.append({'title': title, 'description': _b(p.get(f't-{i}-description')), 'department': _b(p.get(f't-{i}-department')),
                          'assigneeType': at, 'assigneeUserId': _b(p.get(f't-{i}-assigneeUserId')) if at == 'user' else None,
                          'dueOffsetDays': off, 'link': _b(p.get(f't-{i}-link'))})
        return {'kind': kind, 'name': (d['name'] or '').strip(), 'description': d['description'] or '', 'isDefault': d['isDefault'],
                'active': d['active'], 'tasks': tasks}


class AssetForm(PostForm):
    tag = Text()
    name = Text()
    typeId = Text()
    serial = Text()
    condition = Text()
    purchaseDate = Text()
    cost = Text()

    def body(self) -> dict:
        d = self.cleaned()
        cost = (d['cost'] or '').strip()
        if cost:
            try:
                cost = f'{float(cost):.2f}'
            except ValueError:
                pass
        return {'tag': (d['tag'] or '').strip(), 'name': (d['name'] or '').strip(), 'typeId': _b(d['typeId']), 'serial': _b(d['serial']),
                'condition': d['condition'] or 'good', 'purchaseDate': _b(d['purchaseDate']), 'cost': cost or None}


class AssignAssetForm(PostForm):
    employee_id = Text()
    notes = Text()
    assignedOn = Text()

    def body(self) -> dict:
        d = self.cleaned()
        body = {'employeeId': d['employee_id'] or '', 'notes': _b(d['notes'])}
        if _b(d['assignedOn']):
            body['assignedOn'] = d['assignedOn']
        return body


class ReturnAssetForm(PostForm):
    condition = Text()
    notes = Text()
    returnedOn = Text()

    def body(self) -> dict:
        d = self.cleaned()
        body = {'condition': d['condition'] or '', 'notes': _b(d['notes'])}
        if _b(d['returnedOn']):
            body['returnedOn'] = d['returnedOn']
        return body


class SettingsForm(PostForm):
    autoStartOnboarding = Check()
    selfExitInterview = Check()
    noticeDays = Text()
    probationReminderDays = Text()

    def body(self) -> dict:
        d = self.cleaned()
        days = [int(x) for x in (d['probationReminderDays'] or '').replace(' ', '').split(',') if x.isdigit() and 0 < int(x) <= 90]
        try:
            notice = int(d['noticeDays'] or 0)
        except ValueError:
            notice = 0
        return {'autoStartOnboarding': d['autoStartOnboarding'], 'noticeDays': notice, 'probationReminderDays': days[:4],
                'selfExitInterview': d['selfExitInterview']}


class ExitForm(PostForm):
    employee_id = Text()
    kind = Text()
    lastWorkingDay = Text()
    voluntary = Check()
    reasonCategory = Text()
    reason = Text()

    def body(self) -> dict:
        d = self.cleaned()
        return {'employeeId': d['employee_id'] or '', 'kind': d['kind'] or 'resignation', 'lastWorkingDay': d['lastWorkingDay'] or '',
                'voluntary': d['voluntary'], 'reasonCategory': _b(d['reasonCategory']), 'reason': _b(d['reason'])}


class LastDayForm(PostForm):
    lastWorkingDay = Text()

    def body(self) -> dict:
        return {'lastWorkingDay': self.cleaned()['lastWorkingDay'] or ''}


class CancelExitForm(PostForm):
    reason = Text()

    def body(self) -> dict:
        return {'reason': (self.cleaned()['reason'] or '').strip()}


class CompleteExitForm(PostForm):
    skipOpenTasks = Check()

    def body(self) -> dict:
        return {'skipOpenTasks': self.cleaned()['skipOpenTasks']}


class InterviewForm(PostForm):
    """Answers posted as q-<question id>; blanks are skipped; ratings are numbers."""

    def answers(self, questions: list[dict]) -> dict:
        out = []
        for q in questions:
            v = (self.data.get(f"q-{q['id']}") or '').strip()
            if not v:
                continue
            out.append({'questionId': q['id'], 'answer': int(v) if q['kind'] == 'rating' and v.isdigit() else v})
        return {'answers': out}


class QuestionsForm(PostForm):
    """The interview question editor: q-<i>-question / kind / options (comma separated) / active / id."""

    def body(self) -> dict:
        p = self.data
        qs = []
        for i in _rows(p, 'q'):
            text = (p.get(f'q-{i}-question') or '').strip()
            if not text:
                continue
            q = {'question': text, 'kind': p.get(f'q-{i}-kind') or 'text',
                 'options': [o.strip() for o in (p.get(f'q-{i}-options') or '').split(',') if o.strip()],
                 'active': p.get(f'q-{i}-active') in ('on', 'true', '1')}
            if _b(p.get(f'q-{i}-id')):
                q['id'] = p[f'q-{i}-id']
            qs.append(q)
        return {'questions': qs}


class ResignForm(PostForm):
    proposedLastWorkingDay = Text()
    reasonCategory = Text()
    reason = Text()

    def body(self) -> dict:
        d = self.cleaned()
        return {'proposedLastWorkingDay': d['proposedLastWorkingDay'] or '', 'reasonCategory': _b(d['reasonCategory']),
                'reason': _b(d['reason'])}


class AssetFilterForm(PostForm):
    """The asset register's filter bar (GET)."""
    status = Text()
    search = Text()

    def query(self) -> tuple[dict, str, str]:
        d = self.cleaned()
        status = d['status'] or 'all'
        search = (d['search'] or '').strip()
        q = {}
        if status in ('available', 'assigned', 'retired'):
            q['status'] = status
        if search:
            q['search'] = search[:80]
        return q, status, search
