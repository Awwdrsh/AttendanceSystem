"""
Page forms of the people app. A form reads the HTML post (empty inputs, checkboxes, number inputs, repeated rows named
<prefix>-<i>-<field>) and builds the TS-shaped body the service validates with serializers.py, so a page post and a
JSON body are refused with the same messages. Employees and lifecycle pages use the PostForm base too.
"""
from __future__ import annotations

from django import forms

from hajiri.core.errors import NotFoundError, ValidationError


def form_issues(form: forms.Form, names: dict[str, str] | None = None) -> ValidationError:
    """ValidationError with the form's errors as TS issues; `names` maps form fields to body keys."""
    names = names or {}
    return ValidationError([{'path': names.get(f, f) if f != '__all__' else '', 'message': str(m)}
                            for f, msgs in form.errors.items() for m in msgs])


def blank(v):
    """'' and whitespace become None (an empty input clears a value)."""
    return None if v is None or (isinstance(v, str) and v.strip() == '') else v


class Text(forms.CharField):
    """A text input as typed (not stripped); blank is allowed and checked by the service."""

    def __init__(self, **kw):
        kw.setdefault('required', False)
        kw.setdefault('strip', False)
        super().__init__(**kw)


class Check(forms.BooleanField):
    """A checkbox or switch: 'on', 'true' or '1' is ticked."""

    def __init__(self, **kw):
        kw.setdefault('required', False)
        super().__init__(**kw)

    def to_python(self, value):
        return value in (True, 'on', 'true', '1')


class Number(forms.Field):
    """A number input: '' is None, digits are an int, decimals a float; anything else is passed on as typed so the
    service refuses it with its own message."""

    def to_python(self, value):
        v = blank(value)
        if v is None:
            return None
        s = str(v).strip()
        try:
            return int(s) if s.lstrip('-').isdigit() else float(s)
        except ValueError:
            return s

    def __init__(self, **kw):
        kw.setdefault('required', False)
        super().__init__(**kw)


class PostForm(forms.Form):
    """A form that is always bound to a post and raises instead of re-rendering (the pages show a toast)."""
    body_names: dict[str, str] = {}

    def cleaned(self) -> dict:
        if not self.is_valid():
            raise form_issues(self, self.body_names)
        return self.cleaned_data

    def sent(self, name: str) -> bool:
        return name in self.data


def repeated_rows(post, prefix: str, form_class: type[forms.Form]) -> list[dict]:
    """Repeated rows posted as <prefix>-<i>-<field> (each one a form with prefix '<prefix>-<i>'); blank rows are
    dropped. Returns each row's `row()`."""
    idx = sorted({int(k.split('-')[1]) for k in post if k.startswith(prefix + '-') and k.split('-')[1].isdigit()})
    out = []
    for i in idx:
        f = form_class(post, prefix=f'{prefix}-{i}')
        if not f.is_valid():
            raise form_issues(f)
        row = f.row()
        if row is not None:
            out.append(row)
    return out


# ---------------------------------------------------------------- profile sections


class _Row(forms.Form):
    """One repeated row: text fields become None when blank; the row is dropped when nothing but checkboxes is set."""
    checks: tuple[str, ...] = ()

    def row(self) -> dict | None:
        d = {k: (v if k in self.checks else blank(v)) for k, v in self.cleaned_data.items()}
        if not any(d[k] not in (None, False) for k in d if k not in self.checks):
            return None
        return d


class ContactRow(_Row):
    name = Text()
    relation = Text()
    phone = Text()
    altPhone = Text()
    address = Text()


class FamilyRow(_Row):
    checks = ('isDependent', 'isNominee')
    name = Text()
    relation = Text()
    dob = Text()
    occupation = Text()
    phone = Text()
    isDependent = Check()
    isNominee = Check()
    nomineeShare = Number()

    def row(self) -> dict | None:
        d = super().row()
        if d is not None and not d['isNominee']:
            d['nomineeShare'] = None
        return d


class EducationRow(_Row):
    level = Text()
    degree = Text()
    field = Text()
    institution = Text()
    board = Text()
    startYear = Number()
    endYear = Number()
    result = Text()


class ExperienceRow(_Row):
    organisation = Text()
    designation = Text()
    fromDate = Text()
    toDate = Text()
    responsibilities = Text()
    reasonForLeaving = Text()


_ADDRESS = ('province', 'district', 'municipality', 'street')


def _address(post, prefix: str):
    a = {k: blank(post.get(f'{prefix}_{k}')) for k in _ADDRESS}
    ward = blank(post.get(f'{prefix}_ward'))
    if ward is not None:
        a['ward'] = int(ward) if ward.isdigit() else ward
    a = {k: v for k, v in a.items() if v is not None}
    return a or None


def _split(s: str | None) -> list[str]:
    return [x.strip() for x in (s or '').replace('\n', ',').split(',') if x.strip()]


class SectionForm(PostForm):
    """The TS toPayload of ProfileSections.tsx for one section: empty secrets are left out (the stored one is kept),
    empty text clears the value."""
    mode = Text()
    reason = Text()
    proofFileId = Text()

    def __init__(self, section: str, data, **kw):
        super().__init__(data, **kw)
        self.section = section

    def value(self) -> dict:  # noqa: C901 - one branch per section, as the TS form
        post = self.data
        g = lambda k: blank(post.get(k))  # noqa: E731
        section = self.section
        if section == 'personal':
            return {k: g(k) for k in ('gender', 'dob', 'maritalStatus', 'bloodGroup', 'nationality')}
        if section == 'contact':
            return {'personalEmail': g('personalEmail'), 'personalPhone': g('personalPhone'), 'workPhone': g('workPhone'),
                    'permanentAddress': _address(post, 'perm'), 'temporaryAddress': _address(post, 'temp')}
        if section == 'bank':
            out = {'bankCode': (g('bankCode') or '').upper() or None, 'bankBranch': g('bankBranch')}
            acct = (post.get('bankAccount') or '').replace(' ', '').strip()
            if acct:
                out['bankAccount'] = acct
            return out
        if section == 'ids':
            out = {k: g(k) for k in ('citizenshipDistrict', 'citizenshipIssuedOn', 'passportExpiry', 'licenceExpiry', 'ssfId',
                                     'citId', 'pfId')}
            for k in ('pan', 'citizenshipNo', 'passportNo', 'licenceNo'):
                v = (post.get(k) or '').strip()
                if v:
                    out[k] = v.upper() if k == 'passportNo' else v
            return out
        if section == 'emergency':
            return {'contacts': repeated_rows(post, 'c', ContactRow)}
        if section == 'family':
            return {'members': repeated_rows(post, 'm', FamilyRow)}
        if section == 'education':
            return {'items': repeated_rows(post, 'e', EducationRow)}
        if section == 'experience':
            return {'items': repeated_rows(post, 'x', ExperienceRow)}
        if section == 'skills':
            return {'skills': _split(post.get('skills')), 'languages': _split(post.get('languages'))}
        raise NotFoundError(f'unknown profile section {section}')

    def change_request(self) -> dict:
        """The body of POST /people/me/change-requests for this section."""
        d = self.cleaned()
        body = {'section': self.section, 'value': self.value()}
        if (d['reason'] or '').strip():
            body['reason'] = d['reason'].strip()
        if blank(d['proofFileId']):
            body['proofFileId'] = d['proofFileId']
        return body


# ---------------------------------------------------------------- career events


class CareerForm(PostForm):
    """CareerEventForm.tsx: only the details the chosen kind uses are sent; blanks are left out."""
    kind = Text()
    effectiveDate = Text()
    designationId = Text()
    grade = Text()
    branchId = Text()
    orgUnitId = Text()
    managerId = Text()
    employmentType = Text()
    revisionPercent = Text()
    separationType = Text()
    reason = Text()
    note = Text()

    def body(self) -> dict:
        d = {k: blank(v) for k, v in self.cleaned().items()}
        kind = d['kind'] or 'note'
        details: dict = {}
        if kind in ('promotion', 'designation_change') and d['designationId']:
            details['designationId'] = d['designationId']
        if kind in ('promotion', 'grade_change') and d['grade']:
            details['grade'] = d['grade'].strip()
        if kind == 'transfer':
            for k in ('branchId', 'orgUnitId', 'managerId'):
                if d[k]:
                    details[k] = d[k]
        if kind == 'manager_change' and d['managerId']:
            # Left empty, the TS form sends no managerId and the service answers 'choose the new manager'.
            details['managerId'] = d['managerId']
        if kind == 'employment_type_change' and d['employmentType']:
            details['employmentType'] = d['employmentType']
        if kind == 'salary_revision' and d['revisionPercent']:
            try:
                details['revisionPercent'] = float(d['revisionPercent'])
            except ValueError:
                raise ValidationError([{'path': 'details.revisionPercent', 'message': 'Expected number, received string'}]) from None
        if kind == 'separation' and d['separationType']:
            details['separationType'] = d['separationType']
        if kind != 'note' and d['reason']:
            details['reason'] = d['reason'].strip()
        body = {'kind': kind, 'effectiveDate': self.cleaned_data['effectiveDate'] or '', 'details': details}
        if d['note']:
            body['note'] = d['note'].strip()
        return body


# ---------------------------------------------------------------- org structure and policy


class StructureForm(PostForm):
    """The org structure editor (unit, branch or designation): empty optional text becomes null so it clears;
    required text is sent as typed."""
    code = Text()
    nameEn = Text()
    kind = Text()
    parentId = Text()
    branchId = Text()
    headEmployeeId = Text()
    description = Text()
    address = Text()
    phone = Text()
    grade = Text()
    jobLevel = Number()
    orgUnitId = Text()
    active = Check()

    def __init__(self, what: str, data, **kw):
        super().__init__(data, **kw)
        self.what = what

    def body(self) -> dict:
        d = self.cleaned()
        b = lambda k: blank(d[k])  # noqa: E731
        body = {'code': (d['code'] or '').strip().upper(), 'nameEn': (d['nameEn'] or '').strip()}
        if self.what == 'unit':
            body.update({'kind': d['kind'] or 'department', 'parentId': b('parentId'), 'branchId': b('branchId'),
                         'headEmployeeId': b('headEmployeeId'), 'description': b('description')})
        elif self.what == 'branch':
            body.update({'address': b('address'), 'phone': b('phone'), 'headEmployeeId': b('headEmployeeId')})
        else:
            body.update({'grade': b('grade'), 'jobLevel': d['jobLevel'], 'orgUnitId': b('orgUnitId'), 'description': b('description'),
                         'active': d['active']})
        return body


class PolicyForm(PostForm):
    makerChecker = Check()

    def body(self) -> dict:
        return {'makerChecker': self.cleaned()['makerChecker']}


class RejectForm(PostForm):
    reason = Text()

    def reason_text(self) -> str:
        reason = (self.cleaned()['reason'] or '').strip()
        if not reason:
            raise ValidationError([{'path': 'reason', 'message': 'Required'}])
        return reason


class DirectoryForm(PostForm):
    """The directory filter bar (GET): search text, department and branch."""
    search = Text()
    unit = Text()
    branch = Text()

    def query(self) -> dict:
        d = self.cleaned()
        return {'search': (d['search'] or '').strip()[:100] or None, 'orgUnitId': blank(d['unit']), 'branchId': blank(d['branch'])}
