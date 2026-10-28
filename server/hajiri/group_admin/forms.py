"""
Page forms of group administration. Each form knows the HTML side (file inputs, checkboxes, '' for empty) and
builds the camelCase body the services validate with their serializers. Company choices are the actor's group only,
so a foreign company id is a form error before it reaches a service (which would answer 'company not found').
"""
from __future__ import annotations

from django import forms

from hajiri.core.auth import ROLES
from hajiri.web.nav import ROLE_LABEL

from . import services
from .serializers import CODE_RE, PAN_RE

ROLE_CHOICES = [(r, ROLE_LABEL.get(r, r)) for r in ROLES]


def _uuid(v) -> str | None:
    return str(v) if v else None


class _Profile(forms.Form):
    name_ne = forms.CharField(max_length=120, required=False, label='Name in Nepali')
    pan = forms.RegexField(regex=PAN_RE, required=False, label='PAN',
                           help_text='Permanent account number from the Inland Revenue Department (9 digits).',
                           error_messages={'invalid': 'PAN has 9 digits.'})
    address = forms.CharField(max_length=300, required=False, label='Address', widget=forms.Textarea(attrs={'rows': 2}))
    logo = forms.ImageField(required=False, label='Logo', help_text='PNG or JPEG; shown on payslips, letters and the app header.')

    def logo_url(self) -> str | None:
        f = self.cleaned_data.get('logo')
        return services.logo_data_url(f.read()) if f else None


class CompanyForm(_Profile):
    code = forms.RegexField(regex=CODE_RE, label='Code',
                            help_text='Short code, 2 to 20 letters or digits (used in device serials and exports).',
                            error_messages={'invalid': 'Use 2 to 20 letters, digits, - or _.'})
    name_en = forms.CharField(max_length=120, label='Name')
    field_order = ['code', 'name_en', 'name_ne', 'pan', 'address', 'logo']

    def body(self) -> dict:
        d = self.cleaned_data
        return {'code': d['code'], 'nameEn': d['name_en'], 'nameNe': d['name_ne'] or None, 'pan': d['pan'] or None,
                'address': d['address'] or None, 'logoDataUrl': self.logo_url()}


class CompanyEditForm(_Profile):
    name_en = forms.CharField(max_length=120, label='Name')
    remove_logo = forms.BooleanField(required=False, label='Remove the logo')
    field_order = ['name_en', 'name_ne', 'pan', 'address', 'logo', 'remove_logo']

    def body(self) -> dict:
        d = self.cleaned_data
        out = {'nameEn': d['name_en'], 'nameNe': d['name_ne'] or None, 'pan': d['pan'] or None, 'address': d['address'] or None}
        logo = self.logo_url()
        if logo:
            out['logoDataUrl'] = logo
        elif d['remove_logo']:
            out['logoDataUrl'] = None
        return out


class AttachForm(forms.Form):
    company_id = forms.ChoiceField(label='Company')

    def __init__(self, actor, *args, **kw):
        super().__init__(*args, **kw)
        self.fields['company_id'].choices = [(c['id'], f"{c['name']} ({c['code']})") for c in services.attachable_companies(actor)]

    def body(self) -> dict:
        return {'companyId': self.cleaned_data['company_id']}


class _GroupCompanyForm(forms.Form):
    company_id = forms.ChoiceField(label='Company')

    def __init__(self, actor, *args, **kw):
        super().__init__(*args, **kw)
        self.fields['company_id'].choices = services.company_choices(actor)


class AccessForm(_GroupCompanyForm):
    email = forms.EmailField(max_length=200, label='Email of the person')
    roles = forms.MultipleChoiceField(choices=ROLE_CHOICES, label='Roles in this company', widget=forms.CheckboxSelectMultiple)
    employee_id = forms.UUIDField(required=False, label='Employee record in this company')
    field_order = ['company_id', 'email', 'roles', 'employee_id']

    def body(self) -> dict:
        d = self.cleaned_data
        return {'companyId': d['company_id'], 'email': d['email'], 'roles': d['roles'], 'employeeId': _uuid(d['employee_id'])}


class InviteForm(AccessForm):
    name = forms.CharField(max_length=120, label='Name')
    password = forms.CharField(max_length=256, strip=False, label='Temporary password', widget=forms.PasswordInput(render_value=True),
                               help_text='They choose their own at the first sign-in.')
    field_order = ['company_id', 'name', 'email', 'roles', 'employee_id', 'password']

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.fields['company_id'].label = 'Home company'

    def body(self) -> dict:
        return {**super().body(), 'name': self.cleaned_data['name'], 'password': self.cleaned_data['password']}


class TransferForm(forms.Form):
    employee_id = forms.UUIDField(label='Employee')
    to_company_id = forms.ChoiceField(label='Moves to')
    effective_date = forms.DateField(label='First day in the new company', input_formats=['%Y-%m-%d'])
    emp_code = forms.CharField(max_length=40, required=False, label='Employee code there',
                               help_text='Leave empty to keep the same code.')
    designation = forms.CharField(max_length=120, required=False, label='Designation there')
    branch_id = forms.UUIDField(required=False, label='Branch')
    org_unit_id = forms.UUIDField(required=False, label='Department')
    manager_id = forms.UUIDField(required=False, label='Manager')
    shift_id = forms.UUIDField(required=False, label='Shift')
    carry_leave = forms.BooleanField(required=False, label='Carry the leave balance over')
    carry_salary = forms.BooleanField(required=False, label='Keep the same salary (structure with the same code)')
    note = forms.CharField(max_length=500, required=False, label='Note', widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, actor, *args, **kw):
        super().__init__(*args, **kw)
        self.fields['to_company_id'].choices = [('', 'Choose a company')] + [
            (c['id'], c['name']) for c in services.target_companies(actor)]

    def body(self) -> dict:
        d = self.cleaned_data
        return {'employeeId': str(d['employee_id']), 'toCompanyId': d['to_company_id'],
                'effectiveDate': d['effective_date'].isoformat(), 'empCode': d['emp_code'],
                'designation': d['designation'] or None, 'branchId': _uuid(d['branch_id']),
                'orgUnitId': _uuid(d['org_unit_id']), 'managerId': _uuid(d['manager_id']), 'shiftId': _uuid(d['shift_id']),
                'carryLeave': d['carry_leave'], 'carrySalary': d['carry_salary'], 'note': d['note'] or None}


class TransferRuleForm(_GroupCompanyForm):
    carry_leave_balance = forms.BooleanField(required=False)

    def body(self) -> dict:
        return {'companyId': self.cleaned_data['company_id'], 'carryLeaveBalance': self.cleaned_data['carry_leave_balance']}


class PolicyCopyForm(forms.Form):
    source_id = forms.ChoiceField(label='Copy from')
    target_ids = forms.MultipleChoiceField(label='Copy to', widget=forms.CheckboxSelectMultiple)
    sections = forms.MultipleChoiceField(choices=services.SECTION_CHOICES, label='What to copy',
                                         widget=forms.CheckboxSelectMultiple)
    on_conflict = forms.ChoiceField(choices=[('skip', 'Keep what the company has'), ('overwrite', 'Overwrite with the copy')],
                                    initial='skip', label='When a company already has it', widget=forms.RadioSelect)

    def __init__(self, actor, *args, **kw):
        super().__init__(*args, **kw)
        choices = services.company_choices(actor)
        self.fields['source_id'].choices = choices
        self.fields['target_ids'].choices = choices

    def clean(self):
        d = super().clean()
        if d.get('source_id') and d.get('source_id') in (d.get('target_ids') or []):
            self.add_error('target_ids', 'Copy to companies other than the source.')
        return d

    def body(self) -> dict:
        d = self.cleaned_data
        return {'sourceCompanyId': d['source_id'], 'targetCompanyIds': d['target_ids'], 'sections': d['sections'],
                'onConflict': d['on_conflict']}
