"""
The employee master beyond the basics, one schema per profile section (port of apps/api/src/modules/people/profile.ts).
Flat sections are partial (an omitted key is unchanged, null clears it); list sections (emergency, family, education,
experience) replace the whole list. ID numbers, PAN and the bank account are stored encrypted with the field cipher.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.db.models.functions import Now

from hajiri.core.crypto import field_cipher
from hajiri.core.errors import BadRequestError, ConflictError, NotFoundError, ValidationError
from hajiri.core.validation import validate
from hajiri.db.models import (Employee, EmployeeEducation, EmployeeEmergencyContact, EmployeeExperience, EmployeeFamily)

from .history import record_field_changes
from .serializers import EDUCATION_LEVELS, FAMILY_RELATIONS, SECTION_SERIALIZERS  # noqa: F401

BLOOD_GROUPS = tuple(Employee.BloodGroup.values)

# "Left out" in a section value (the stored value is kept), as opposed to None (cleared).
MISSING: Any = type('Missing', (), {'__repr__': lambda s: 'MISSING', '__bool__': lambda s: False})()

SECTIONS = tuple(SECTION_SERIALIZERS)
SECTION_LABEL = {
    'personal': 'Personal details', 'contact': 'Contact and address', 'bank': 'Bank account', 'ids': 'ID documents',
    'emergency': 'Emergency contacts', 'family': 'Family and nominees', 'education': 'Education',
    'experience': 'Previous employment', 'skills': 'Skills and languages',
}
# Sections whose direct edits by HR need a second HR's approval when people.policy.makerChecker is on.
SENSITIVE_SECTIONS = ('bank', 'ids')


def _plain(v: Any) -> Any:
    """Validated data as plain dicts and lists (what the TS code stores as JSON)."""
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def parse_section(section: str, value: Any) -> tuple[str, dict]:
    if section not in SECTION_SERIALIZERS:
        raise NotFoundError(f'unknown profile section {section}')
    if value is None or value is MISSING:
        raise ValidationError([{'path': '', 'message': 'Required'}])
    return section, _plain(validate(SECTION_SERIALIZERS[section], value))


# ---------------------------------------------------------------- reading

FIELD_LABEL = {
    'gender': 'Gender', 'dob': 'Date of birth', 'maritalStatus': 'Marital status', 'bloodGroup': 'Blood group', 'nationality': 'Nationality',
    'personalEmail': 'Personal email', 'personalPhone': 'Personal phone', 'workPhone': 'Work phone', 'permanentAddress': 'Permanent address',
    'temporaryAddress': 'Temporary address', 'bankCode': 'Bank', 'bankBranch': 'Bank branch', 'bankAccount': 'Bank account', 'pan': 'PAN',
    'citizenshipNo': 'Citizenship number', 'citizenshipDistrict': 'Citizenship issue district', 'citizenshipIssuedOn': 'Citizenship issue date',
    'passportNo': 'Passport number', 'passportExpiry': 'Passport expiry', 'licenceNo': 'Driving licence number',
    'licenceExpiry': 'Driving licence expiry', 'ssfId': 'SSF ID', 'citId': 'CIT ID', 'pfId': 'PF number',
    'contacts': 'Emergency contacts', 'members': 'Family members', 'skills': 'Skills', 'languages': 'Languages',
}
LIST_LABEL = {'education': 'Education', 'experience': 'Previous employment'}


def field_label(f: str, section: str | None = None) -> str:
    if f == 'items' and section in LIST_LABEL:
        return LIST_LABEL[section]
    return FIELD_LABEL.get(f, f)


# Encrypted ID numbers: column and AAD suffix. PAN and bank keep the AADs the employees module already uses.
SECRET_COLS = {
    'pan': ('pan_enc', 'pan'), 'bankAccount': ('bank_account_enc', 'bank'),
    'citizenshipNo': ('citizenship_no_enc', 'citizenship'), 'passportNo': ('passport_no_enc', 'passport'),
    'licenceNo': ('licence_no_enc', 'licence'),
}
SECRET_FIELDS = tuple(SECRET_COLS)


def secret_aad(employee_id: str, f: str) -> str:
    return f'employee:{employee_id}:{SECRET_COLS[f][1]}'


def mask(v: str | None) -> str | None:
    if not v:
        return None
    return '••••' if len(v) <= 4 else f'•••• {v[-4:]}'


def _iso(v):
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def js_num(v):
    """Number(v) of a numeric column: integral values as int, so JSON and text match the TS output."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    f = float(v) if isinstance(v, (Decimal, str)) else v
    if isinstance(f, float) and f.is_integer():
        return int(f)
    return f


_PROFILE_COLS = ('id', 'gender', 'dob', 'marital_status', 'blood_group', 'nationality', 'personal_email', 'personal_phone',
                 'work_phone', 'permanent_address', 'temporary_address', 'bank_code', 'bank_branch', 'bank_account_last4',
                 'ssf_id', 'cit_id', 'pf_id', 'citizenship_district', 'citizenship_issued_on', 'passport_expiry',
                 'licence_expiry', 'skills', 'languages', 'pan_enc', 'bank_account_enc', 'citizenship_no_enc',
                 'passport_no_enc', 'licence_no_enc')


def _load_emp(employee_id: str) -> dict:
    r = Employee.objects.filter(pk=employee_id).values(*_PROFILE_COLS).first()
    if not r:
        raise NotFoundError('employee not found')
    return r


def read_profile(employee_id: str, *, reveal: bool = False) -> dict:
    """The whole profile, section by section. Encrypted numbers are masked unless `reveal` (the caller audits that).
    Callers have already checked that the employee belongs to the active company."""
    employee_id = str(employee_id)
    e = _load_emp(employee_id)
    cipher = field_cipher()

    def secret(f: str) -> str | None:
        blob = e[SECRET_COLS[f][0]]
        if not blob:
            return None
        plain = cipher.decrypt(bytes(blob), secret_aad(employee_id, f))
        return plain if reveal else mask(plain)
    contacts = EmployeeEmergencyContact.objects.filter(employee_id=employee_id).order_by('sort', 'name').values(
        'name', 'relation', 'phone', 'alt_phone', 'address')
    family = EmployeeFamily.objects.filter(employee_id=employee_id).order_by('sort', 'name').values(
        'name', 'relation', 'dob', 'occupation', 'phone', 'is_dependent', 'is_nominee', 'nominee_share')
    education = EmployeeEducation.objects.filter(employee_id=employee_id).order_by('sort').values(
        'level', 'degree', 'field', 'institution', 'board', 'start_year', 'end_year', 'result')
    experience = EmployeeExperience.objects.filter(employee_id=employee_id).order_by('sort').values(
        'organisation', 'designation', 'from_date', 'to_date', 'responsibilities', 'reason_for_leaving')
    return {
        'personal': {'gender': e['gender'], 'dob': _iso(e['dob']), 'maritalStatus': e['marital_status'], 'bloodGroup': e['blood_group'],
                     'nationality': e['nationality']},
        'contact': {'personalEmail': e['personal_email'], 'personalPhone': e['personal_phone'], 'workPhone': e['work_phone'],
                    'permanentAddress': e['permanent_address'], 'temporaryAddress': e['temporary_address']},
        'bank': {'bankCode': e['bank_code'], 'bankBranch': e['bank_branch'], 'bankAccount': secret('bankAccount')},
        'ids': {
            'pan': secret('pan'), 'citizenshipNo': secret('citizenshipNo'), 'citizenshipDistrict': e['citizenship_district'],
            'citizenshipIssuedOn': _iso(e['citizenship_issued_on']), 'passportNo': secret('passportNo'),
            'passportExpiry': _iso(e['passport_expiry']), 'licenceNo': secret('licenceNo'), 'licenceExpiry': _iso(e['licence_expiry']),
            'ssfId': e['ssf_id'], 'citId': e['cit_id'], 'pfId': e['pf_id'],
        },
        'emergency': {'contacts': [{'name': c['name'], 'relation': c['relation'], 'phone': c['phone'], 'altPhone': c['alt_phone'],
                                    'address': c['address']} for c in contacts]},
        'family': {'members': [{
            'name': m['name'], 'relation': m['relation'], 'dob': _iso(m['dob']), 'occupation': m['occupation'], 'phone': m['phone'],
            'isDependent': m['is_dependent'], 'isNominee': m['is_nominee'], 'nomineeShare': js_num(m['nominee_share']),
        } for m in family]},
        'education': {'items': [{'level': x['level'], 'degree': x['degree'], 'field': x['field'], 'institution': x['institution'],
                                 'board': x['board'], 'startYear': x['start_year'], 'endYear': x['end_year'], 'result': x['result']}
                                for x in education]},
        'experience': {'items': [{'organisation': x['organisation'], 'designation': x['designation'], 'fromDate': _iso(x['from_date']),
                                  'toDate': _iso(x['to_date']), 'responsibilities': x['responsibilities'],
                                  'reasonForLeaving': x['reason_for_leaving']} for x in experience]},
        'skills': {'skills': list(e['skills'] or []), 'languages': list(e['languages'] or [])},
    }


# ---------------------------------------------------------------- diffing

def _js(v) -> str:
    """String(v) in JavaScript for scalars."""
    if v is None:
        return 'null'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float, Decimal)):
        n = js_num(v)
        return str(n)
    return str(v)


def display(field: str, v: Any) -> str | None:
    """A readable one-line form of any section value, for history and the approval inbox."""
    if v is None or v == '':
        return None
    if isinstance(v, (list, tuple)):
        if not v:
            return None
        if not isinstance(v[0], dict):
            return ', '.join(_js(x) for x in v)
        parts = []
        for o in v:
            head = next((_js(o[k]) for k in ('name', 'institution', 'organisation') if o.get(k) is not None), '')
            tail = next((_js(o[k]) for k in ('relation', 'degree', 'level', 'designation') if o.get(k) is not None), '').replace('_', ' ')
            share = f", nominee {_js(o['nomineeShare'])}%" if o.get('isNominee') and o.get('nomineeShare') else ''
            parts.append(f'{head} ({tail}{share})' if tail else head)
        return '; '.join(parts)
    if isinstance(v, dict):
        a = v
        bits = [a.get('street'), f"Ward {_js(a['ward'])}" if a.get('ward') else None, a.get('municipality'), a.get('district'), a.get('province')]
        return ', '.join(_js(x) for x in bits if x is not None and x != '') or None
    if field in ('gender', 'maritalStatus'):
        s = str(v)
        return s[:1].upper() + s[1:]
    return _js(v)


def normalise(v: Any) -> Any:
    if v is MISSING or v == '':
        return None
    if isinstance(v, (list, tuple)):
        return [normalise(x) for x in v]
    if isinstance(v, dict):
        items = [(k, normalise(x)) for k, x in v.items()]
        return {k: x for k, x in sorted(items) if x is not None and x is not False}
    if isinstance(v, (Decimal, float)) and not isinstance(v, bool):
        return js_num(v)
    return v


def _same(a, b) -> bool:
    return json.dumps(normalise(a), ensure_ascii=False, default=str) == json.dumps(normalise(b), ensure_ascii=False, default=str)


def diff_section(employee_id: str, section: str, value: dict, *, reveal: bool = False) -> list[dict]:
    """Field-by-field differences between the stored section and a proposed value. Secret numbers are compared in
    clear and shown masked unless `reveal`."""
    current = read_profile(employee_id, reveal=True)[section]
    out = []
    for k, nxt in value.items():
        if nxt is MISSING:
            continue
        before = current.get(k)
        if _same(before, nxt):
            continue
        hide = k in SECRET_COLS and not reveal
        frm, to = display(k, before), display(k, nxt)
        out.append({'field': k, 'label': field_label(k, section), 'from': mask(frm) if hide else frm, 'to': mask(to) if hide else to})
    return out


# ---------------------------------------------------------------- writing

def apply_section(*, company_id: str, employee_id: str, section: str, value: dict, actor_id: str | None, source: str,
                  ref_id: str | None = None) -> list[dict]:
    """Write a validated section value and record field history. Call inside a transaction. Returns what changed."""
    employee_id = str(employee_id)
    changes = diff_section(employee_id, section, value)
    if not changes:
        return []
    v = value
    cipher = field_cipher()
    U = MISSING

    def g(k):
        return v.get(k, U)

    def set_(cols: dict) -> None:
        sets = {c: x for c, x in cols.items() if x is not U}
        if sets:
            Employee.objects.filter(pk=employee_id).update(**sets, updated_at=Now())

    def enc(f: str, plain):
        if plain is U:
            return U
        if plain is None:
            return None
        return cipher.encrypt(str(plain), secret_aad(employee_id, f))

    def replace(model, items: list[dict], build) -> None:
        """List sections replace the whole list, keeping the posted order."""
        model.objects.filter(employee_id=employee_id).delete()
        model.objects.bulk_create([model(employee_id=employee_id, sort=i, **build(x)) for i, x in enumerate(items)])

    if section == 'personal':
        set_({'gender': g('gender'), 'dob': g('dob'), 'marital_status': g('maritalStatus'), 'blood_group': g('bloodGroup'),
              'nationality': g('nationality')})
    elif section == 'contact':
        set_({'personal_email': g('personalEmail'), 'personal_phone': g('personalPhone'), 'work_phone': g('workPhone'),
              'permanent_address': g('permanentAddress'), 'temporary_address': g('temporaryAddress')})
    elif section == 'bank':
        acct = g('bankAccount')
        set_({'bank_code': g('bankCode'), 'bank_branch': g('bankBranch'), 'bank_account_enc': enc('bankAccount', acct),
              'bank_account_last4': U if acct is U else str(acct)[-4:]})
    elif section == 'ids':
        pan = g('pan')
        if isinstance(pan, str):
            owner = (Employee.objects.for_company(company_id).filter(pan_bidx=cipher.blind_index(pan)).exclude(pk=employee_id)
                     .values_list('emp_code', flat=True).first())
            if owner:
                raise ConflictError(f'this PAN is already recorded for employee {owner}')
        set_({
            'pan_enc': enc('pan', pan), 'pan_bidx': U if pan is U else None if pan is None else cipher.blind_index(str(pan)),
            'citizenship_no_enc': enc('citizenshipNo', g('citizenshipNo')), 'citizenship_district': g('citizenshipDistrict'),
            'citizenship_issued_on': g('citizenshipIssuedOn'), 'passport_no_enc': enc('passportNo', g('passportNo')),
            'passport_expiry': g('passportExpiry'), 'licence_no_enc': enc('licenceNo', g('licenceNo')), 'licence_expiry': g('licenceExpiry'),
            'ssf_id': g('ssfId'), 'cit_id': g('citId'), 'pf_id': g('pfId'),
        })
    elif section == 'emergency':
        replace(EmployeeEmergencyContact, v['contacts'], lambda c: {
            'name': c['name'], 'relation': c['relation'], 'phone': c['phone'], 'alt_phone': c.get('altPhone'), 'address': c.get('address')})
    elif section == 'family':
        replace(EmployeeFamily, v['members'], lambda m: {
            'name': m['name'], 'relation': m['relation'], 'dob': m.get('dob'), 'occupation': m.get('occupation'), 'phone': m.get('phone'),
            'is_dependent': m.get('isDependent', False), 'is_nominee': m.get('isNominee', False),
            'nominee_share': m.get('nomineeShare') if m.get('isNominee') else None})
    elif section == 'education':
        replace(EmployeeEducation, v['items'], lambda x: {
            'level': x['level'], 'degree': x.get('degree'), 'field': x.get('field'), 'institution': x['institution'],
            'board': x.get('board'), 'start_year': x.get('startYear'), 'end_year': x.get('endYear'), 'result': x.get('result')})
    elif section == 'experience':
        replace(EmployeeExperience, v['items'], lambda x: {
            'organisation': x['organisation'], 'designation': x.get('designation'), 'from_date': x.get('fromDate'),
            'to_date': x.get('toDate'), 'responsibilities': x.get('responsibilities'), 'reason_for_leaving': x.get('reasonForLeaving')})
    elif section == 'skills':
        set_({'skills': g('skills'), 'languages': g('languages')})
    else:
        raise BadRequestError('unknown section')
    record_field_changes(company_id=company_id, employee_id=employee_id, changes=changes, source=source, ref_id=ref_id,
                         actor_id=actor_id)
    return changes
