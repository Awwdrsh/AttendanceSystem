"""
Page forms of the task boards. A form reads the HTML post (checkboxes, repeated inputs, empty selects) and gives the
service its TS-shaped body; the rules themselves (lengths, choices, column checks) live once in serializers.py and
services.py, so a page post and a JSON call are refused with the same messages.
"""
from __future__ import annotations

from django import forms

from hajiri.core.errors import ValidationError

MAX_LABELS = 8
LABEL_LENGTH = 24


class ValuesField(forms.Field):
    """A repeated input (`name=a&name=b`) as a list of strings; blanks dropped unless keep_blank."""
    widget = forms.MultipleHiddenInput

    def __init__(self, *, keep_blank: bool = False, **kw):
        self.keep_blank = keep_blank
        kw.setdefault('required', False)
        super().__init__(**kw)

    def to_python(self, value):
        values = list(value or [])
        return values if self.keep_blank else [v for v in values if v and v.strip()]


class _Form(forms.Form):
    """Every field is optional and read leniently: the service validates the body it is given."""

    def cleaned(self) -> dict:
        """The cleaned post; a value no field accepts (a NUL character in a text box) is refused as a 400 instead
        of leaving the key out of cleaned_data."""
        if not self.is_valid():
            raise ValidationError([{'path': [name], 'message': str(errs[0])} for name, errs in self.errors.items()])
        return self.cleaned_data

    def sent(self, name: str) -> bool:
        return name in self.data


class BoardForm(_Form):
    """New team board: a name and 'Add the people who report to me'."""
    name = forms.CharField(required=False)
    add_my_reports = forms.BooleanField(required=False)

    def body(self) -> dict:
        d = self.cleaned()
        return {'name': d['name'], 'addMyReports': d['add_my_reports']}


class BoardSettingsForm(_Form):
    """Board settings: the name, the columns as parallel col_id/col_name inputs with col_done checkboxes, where tasks
    of removed columns go, and (team boards) the people on it."""
    name = forms.CharField(required=False)
    col_id = ValuesField(keep_blank=True)
    col_name = ValuesField(keep_blank=True)
    col_done = ValuesField()
    move_to = forms.CharField(required=False)
    member_ids = ValuesField()

    def board(self) -> dict:
        d = self.cleaned()
        done = set(d['col_done'])
        body: dict = {'name': d['name'],
                      'columns': [{'id': i, 'name': n, 'done': i in done} for i, n in zip(d['col_id'], d['col_name'])]}
        if d['move_to']:
            body['moveTo'] = d['move_to']
        return body

    def members(self) -> dict:
        return {'userIds': self.cleaned()['member_ids']}


class QuickAddForm(_Form):
    """The card at the bottom of a column: a title and the column it goes in."""
    title = forms.CharField(required=False)
    column_id = forms.CharField(required=False)

    def task(self) -> dict | None:
        """The new task's body, or None when the title is empty (the page ignores an empty quick add)."""
        d = self.cleaned()
        if not d['title']:
            return None
        return {'title': d['title'], **({'columnId': d['column_id']} if d['column_id'] else {})}


class MoveForm(_Form):
    """Drag and drop: a card dropped on a column."""
    task_id = forms.CharField(required=False)
    column_id = forms.CharField(required=False)


class TaskUpdateForm(_Form):
    """The drawer saves one field at a time: title, description, column, priority, due date, labels, assignees.
    Only the fields in the post are changed; `labels_sent` / `assignees_sent` mark the repeated inputs as sent even
    when no box is left."""
    title = forms.CharField(required=False)
    description = forms.CharField(required=False, strip=False)
    column_id = forms.CharField(required=False)
    priority = forms.CharField(required=False)
    due_date = forms.CharField(required=False)
    labels = ValuesField()
    label_new = forms.CharField(required=False)
    remove_label = forms.CharField(required=False, strip=False)
    assignee_ids = ValuesField()

    def empty_title(self) -> bool:
        """A title sent blank: the drawer only saves a non-empty title."""
        return self.sent('title') and not self.cleaned()['title']

    def patch(self) -> dict:
        d = self.cleaned()
        body: dict = {}
        if self.sent('title'):
            body['title'] = d['title']
        if self.sent('description'):
            body['description'] = d['description'] or None
        if self.sent('column_id'):
            body['columnId'] = d['column_id']
        if self.sent('priority'):
            body['priority'] = d['priority']
        if self.sent('due_date'):
            body['dueDate'] = d['due_date'] or None
        if self.sent('labels_sent'):
            labels = list(d['labels'])
            new = d['label_new']
            if new and new not in labels and len(labels) < MAX_LABELS:
                labels.append(new[:LABEL_LENGTH])
            if self.sent('remove_label'):
                labels = [x for x in labels if x != d['remove_label']]
            body['labels'] = [x[:LABEL_LENGTH] for x in labels]
        if self.sent('assignees_sent'):
            body['assigneeIds'] = d['assignee_ids']
        return body


class ChecklistForm(_Form):
    text = forms.CharField(required=False)
    done = forms.BooleanField(required=False)


class CommentForm(_Form):
    body = forms.CharField(required=False)


class AttachmentForm(_Form):
    file_id = forms.CharField(required=False)
