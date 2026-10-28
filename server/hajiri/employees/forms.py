"""
Page forms of the employees hub. EmployeeForm reads EmployeeForm.tsx's post into the TS body the service validates
(serializers.EmployeeIn); ImportForm reads the CSV text or file.
"""
from __future__ import annotations

from django import forms

from hajiri.people.forms import Check, PostForm, Text

TEXT_FIELDS = ('empCode', 'nameEn', 'email', 'phone', 'gender', 'dob', 'maritalStatus', 'designation', 'grade', 'joinDate',
               'probationEnd', 'employmentType', 'branchId', 'exitDate', 'shiftId', 'regime', 'ssfId', 'pfId', 'citId', 'pan',
               'remoteAreaClass', 'bankCode', 'bankBranch', 'bankAccount')
SWITCHES = ('workFromHome', 'otEligible', 'isResident', 'disability')


class WeekdaysField(forms.Field):
    """Repeated weeklyOffDays checkboxes (0 = Sunday): the ticked days, sorted; anything not a digit is ignored."""
    widget = forms.CheckboxSelectMultiple

    def to_python(self, value):
        return sorted(int(x) for x in (value or []) if str(x).isdigit())


class EmployeeForm(PostForm):
    """EmployeeForm.tsx submit(): empty values are left out; switches are always sent; a cleared exit date becomes
    null (when the employee had one)."""
    festivalMonth = Text()
    weeklyOffDays = WeekdaysField(required=False)

    def __init__(self, data, *, initial_row: dict | None = None, **kw):
        super().__init__(data, **kw)
        self.initial_row = initial_row
        for k in TEXT_FIELDS:
            self.fields[k] = Text()
        for k in SWITCHES:
            self.fields[k] = Check()

    def body(self) -> dict:
        d = self.cleaned()
        out: dict = {k: d[k].strip() for k in TEXT_FIELDS if (d[k] or '').strip()}
        for k in SWITCHES:
            out[k] = d[k]
        if d['festivalMonth']:
            fm = d['festivalMonth']
            out['festivalMonth'] = int(fm) if fm.strip().lstrip('-').isdigit() else fm
        out['weeklyOffDays'] = d['weeklyOffDays']
        if self.initial_row and 'exitDate' not in out and self.initial_row.get('exit_date'):
            out['exitDate'] = None
        return out


class ImportForm(PostForm):
    csv = Text()
    action = Text()
    file = forms.FileField(required=False)

    def text(self) -> str:
        d = self.cleaned()
        if d.get('file'):
            return d['file'].read().decode('utf-8-sig', errors='replace')
        return d['csv'] or ''

    @property
    def dry_run(self) -> bool:
        return self.cleaned()['action'] != 'import'
