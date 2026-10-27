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

from hajiri.core.crypto import field_cipher
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, NotFoundError, ValidationError
from hajiri.core.schema import MISSING, Arr, Bool, Enum, Int, IsoDate, Num, Obj, Str, parse

from .history import record_field_changes


def _text(n: int) -> Str:
    return Str(trim=True, min=1, max=n)


def _opt_text(n: int) -> Str:
    return Str(trim=True, max=n, nullish=True)


_PHONE_RE = r'^\+?[0-9 ()-]{6,20}$'


def _phone(**kw) -> Str:
    return Str(trim=True, regex=_PHONE_RE, regex_message='phone number', **kw)


ADDRESS = Obj({
    'province': _opt_text(60), 'district': _opt_text(60), 'municipality': _opt_text(80),
    'ward': Int(min=1, max=40, nullish=True), 'street': _opt_text(160),
}, strict=True, nullish=True)

BLOOD_GROUPS = ('A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-')
FAMILY_RELATIONS = ('spouse', 'father', 'mother', 'son', 'daughter', 'brother', 'sister', 'father_in_law', 'mother_in_law',
                    'grandfather', 'grandmother', 'other')
EDUCATION_LEVELS = ('see', 'plus_two', 'diploma', 'bachelor', 'master', 'mphil', 'phd', 'other')


def _year(**kw) -> Num:
    return Int(min=1950, max=2200, **kw)


SECTION_SCHEMAS: dict[str, Obj] = {
    'personal': Obj({
        'gender': Enum(('male', 'female', 'other'), nullish=True),
        'dob': IsoDate(nullish=True),
        'maritalStatus': Enum(('single', 'couple'), nullish=True),
        'bloodGroup': Enum(BLOOD_GROUPS, nullish=True),
        'nationality': _opt_text(60),
    }, strict=True),
    'contact': Obj({
        'personalEmail': Str(email=True, max=160, nullish=True),
        'personalPhone': _phone(nullish=True),
        'workPhone': _phone(nullish=True),
        'permanentAddress': ADDRESS,
        'temporaryAddress': ADDRESS,
    }, strict=True),
    'bank': Obj({
        'bankCode': _opt_text(20),
        'bankBranch': _opt_text(80),
        'bankAccount': Str(trim=True, regex=r'^[0-9A-Za-z-]{4,40}$', regex_message='account number', optional=True),
    }, strict=True),
    'ids': Obj({
        'pan': Str(trim=True, regex=r'^\d{9}$', regex_message='PAN must be 9 digits', nullish=True),
        'citizenshipNo': Str(trim=True, regex=r'^[0-9A-Za-z/ -]{3,30}$', regex_message='citizenship number', nullish=True),
        'citizenshipDistrict': _opt_text(60),
        'citizenshipIssuedOn': IsoDate(nullish=True),
        'passportNo': Str(trim=True, regex=r'^[0-9A-Za-z]{5,20}$', regex_message='passport number', nullish=True),
        'passportExpiry': IsoDate(nullish=True),
        'licenceNo': Str(trim=True, regex=r'^[0-9A-Za-z/ -]{4,30}$', regex_message='licence number', nullish=True),
        'licenceExpiry': IsoDate(nullish=True),
        'ssfId': _opt_text(20),
        'citId': _opt_text(40),
        'pfId': _opt_text(40),
    }, strict=True),
    'emergency': Obj({
        'contacts': Arr(Obj({'name': _text(120), 'relation': _text(40), 'phone': _phone(), 'altPhone': _phone(nullish=True),
                             'address': _opt_text(200)}, strict=True), max=5),
    }, strict=True),
    'family': Obj({
        'members': Arr(Obj({
            'name': _text(120), 'relation': Enum(FAMILY_RELATIONS), 'dob': IsoDate(nullish=True), 'occupation': _opt_text(80),
            'phone': _phone(nullish=True), 'isDependent': Bool(default=False), 'isNominee': Bool(default=False),
            'nomineeShare': Num(max=100, nullish=True, refine=(lambda v: v > 0, 'Too small: expected number to be >0')),
        }, strict=True), max=20),
    }, strict=True),
    'education': Obj({
        'items': Arr(Obj({
            'level': Enum(EDUCATION_LEVELS), 'degree': _opt_text(120), 'field': _opt_text(120), 'institution': _text(160),
            'board': _opt_text(160), 'startYear': _year(nullish=True), 'endYear': _year(nullish=True), 'result': _opt_text(40),
        }, strict=True, refine=(lambda e: not e.get('startYear') or not e.get('endYear') or e['endYear'] >= e['startYear'],
                                'end year is before start year')), max=15),
    }, strict=True),
    'experience': Obj({
        'items': Arr(Obj({
            'organisation': _text(160), 'designation': _opt_text(120), 'fromDate': IsoDate(nullish=True), 'toDate': IsoDate(nullish=True),
            'responsibilities': _opt_text(1000), 'reasonForLeaving': _opt_text(300),
        }, strict=True, refine=(lambda e: not e.get('fromDate') or not e.get('toDate') or e['toDate'] >= e['fromDate'],
                                'end date is before start date')), max=20),
    }, strict=True),
    'skills': Obj({
        'skills': Arr(Str(trim=True, min=1, max=60), max=30),
        'languages': Arr(Str(trim=True, min=1, max=40), max=15),
    }, strict=True),
}

SECTIONS = tuple(SECTION_SCHEMAS)
SECTION_LABEL = {
    'personal': 'Personal details', 'contact': 'Contact and address', 'bank': 'Bank account', 'ids': 'ID documents',
    'emergency': 'Emergency contacts', 'family': 'Family and nominees', 'education': 'Education',
    'experience': 'Previous employment', 'skills': 'Skills and languages',
}
# Sections whose direct edits by HR need a second HR's approval when people.policy.makerChecker is on.
SENSITIVE_SECTIONS = ('bank', 'ids')


def _family_issues(v: dict) -> list[dict]:
    members = v.get('members') or []
    nominees = [m for m in members if m.get('isNominee')]
    issues = []
    if any(not m.get('nomineeShare') for m in nominees):
        issues.append({'path': 'members', 'message': 'every nominee needs a share'})
    elif nominees and abs(sum(m.get('nomineeShare') or 0 for m in nominees) - 100) > 0.001:
        issues.append({'path': 'members', 'message': 'nominee shares must add up to 100%'})
    if any(not m.get('isNominee') and m.get('nomineeShare') for m in members):
        issues.append({'path': 'members', 'message': 'only nominees have a share'})
    return issues


def parse_section(section: str, value: Any) -> tuple[str, dict]:
    if section not in SECTION_SCHEMAS:
        raise NotFoundError(f'unknown profile section {section}')
    out = parse(SECTION_SCHEMAS[section], value)
    if section == 'family':
        issues = _family_issues(out)
        if issues:
            raise ValidationError(issues)
    return section, out


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


def _load_emp(employee_id: str) -> dict:
    r = one("""SELECT id, gender, dob, marital_status, blood_group, nationality, personal_email, personal_phone, work_phone,
                      permanent_address, temporary_address, bank_code, bank_branch, bank_account_last4, ssf_id, cit_id, pf_id,
                      citizenship_district, citizenship_issued_on, passport_expiry, licence_expiry, skills, languages,
                      pan_enc, bank_account_enc, citizenship_no_enc, passport_no_enc, licence_no_enc
                 FROM employee WHERE id = %s""", [employee_id])
    if not r:
        raise NotFoundError('employee not found')
    return r


def read_profile(employee_id: str, *, reveal: bool = False) -> dict:
    """The whole profile, section by section. Encrypted numbers are masked unless `reveal` (the caller audits that)."""
    employee_id = str(employee_id)
    e = _load_emp(employee_id)
    cipher = field_cipher()

    def secret(f: str) -> str | None:
        blob = e[SECRET_COLS[f][0]]
        if not blob:
            return None
        plain = cipher.decrypt(bytes(blob), secret_aad(employee_id, f))
        return plain if reveal else mask(plain)
    contacts = rows('SELECT name, relation, phone, alt_phone, address FROM employee_emergency_contact WHERE employee_id = %s ORDER BY sort, name', [employee_id])
    family = rows("""SELECT name, relation, dob, occupation, phone, is_dependent, is_nominee, nominee_share FROM employee_family
                      WHERE employee_id = %s ORDER BY sort, name""", [employee_id])
    education = rows("""SELECT level, degree, field, institution, board, start_year, end_year, result FROM employee_education
                         WHERE employee_id = %s ORDER BY sort""", [employee_id])
    experience = rows("""SELECT organisation, designation, from_date, to_date, responsibilities, reason_for_leaving FROM employee_experience
                          WHERE employee_id = %s ORDER BY sort""", [employee_id])
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
        entries = [(c, x) for c, x in cols.items() if x is not U]
        if not entries:
            return
        execute(f"UPDATE employee SET {', '.join(f'{c} = %s' for c, _ in entries)}, updated_at = now() WHERE id = %s",
                [x for _, x in entries] + [employee_id])

    def enc(f: str, plain):
        if plain is U:
            return U
        if plain is None:
            return None
        return cipher.encrypt(str(plain), secret_aad(employee_id, f))

    def js(x):
        if x is U:
            return U
        return None if x is None else json.dumps(x)

    if section == 'personal':
        set_({'gender': g('gender'), 'dob': g('dob'), 'marital_status': g('maritalStatus'), 'blood_group': g('bloodGroup'),
              'nationality': g('nationality')})
    elif section == 'contact':
        set_({'personal_email': g('personalEmail'), 'personal_phone': g('personalPhone'), 'work_phone': g('workPhone'),
              'permanent_address': js(g('permanentAddress')), 'temporary_address': js(g('temporaryAddress'))})
    elif section == 'bank':
        acct = g('bankAccount')
        set_({'bank_code': g('bankCode'), 'bank_branch': g('bankBranch'), 'bank_account_enc': enc('bankAccount', acct),
              'bank_account_last4': U if acct is U else str(acct)[-4:]})
    elif section == 'ids':
        pan = g('pan')
        if isinstance(pan, str):
            owner = one('SELECT emp_code FROM employee WHERE company_id = %s AND pan_bidx = %s AND id <> %s LIMIT 1',
                        [company_id, cipher.blind_index(pan), employee_id])
            if owner:
                raise ConflictError(f"this PAN is already recorded for employee {owner['emp_code']}")
        set_({
            'pan_enc': enc('pan', pan), 'pan_bidx': U if pan is U else None if pan is None else cipher.blind_index(str(pan)),
            'citizenship_no_enc': enc('citizenshipNo', g('citizenshipNo')), 'citizenship_district': g('citizenshipDistrict'),
            'citizenship_issued_on': g('citizenshipIssuedOn'), 'passport_no_enc': enc('passportNo', g('passportNo')),
            'passport_expiry': g('passportExpiry'), 'licence_no_enc': enc('licenceNo', g('licenceNo')), 'licence_expiry': g('licenceExpiry'),
            'ssf_id': g('ssfId'), 'cit_id': g('citId'), 'pf_id': g('pfId'),
        })
    elif section == 'emergency':
        execute('DELETE FROM employee_emergency_contact WHERE employee_id = %s', [employee_id])
        for i, c in enumerate(v['contacts']):
            execute("""INSERT INTO employee_emergency_contact (employee_id, name, relation, phone, alt_phone, address, sort)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    [employee_id, c['name'], c['relation'], c['phone'], c.get('altPhone'), c.get('address'), i])
    elif section == 'family':
        execute('DELETE FROM employee_family WHERE employee_id = %s', [employee_id])
        for i, m in enumerate(v['members']):
            execute("""INSERT INTO employee_family (employee_id, name, relation, dob, occupation, phone, is_dependent, is_nominee, nominee_share, sort)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [employee_id, m['name'], m['relation'], m.get('dob'), m.get('occupation'), m.get('phone'),
                     m.get('isDependent', False), m.get('isNominee', False), m.get('nomineeShare') if m.get('isNominee') else None, i])
    elif section == 'education':
        execute('DELETE FROM employee_education WHERE employee_id = %s', [employee_id])
        for i, x in enumerate(v['items']):
            execute("""INSERT INTO employee_education (employee_id, level, degree, field, institution, board, start_year, end_year, result, sort)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [employee_id, x['level'], x.get('degree'), x.get('field'), x['institution'], x.get('board'), x.get('startYear'),
                     x.get('endYear'), x.get('result'), i])
    elif section == 'experience':
        execute('DELETE FROM employee_experience WHERE employee_id = %s', [employee_id])
        for i, x in enumerate(v['items']):
            execute("""INSERT INTO employee_experience (employee_id, organisation, designation, from_date, to_date, responsibilities, reason_for_leaving, sort)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    [employee_id, x['organisation'], x.get('designation'), x.get('fromDate'), x.get('toDate'), x.get('responsibilities'),
                     x.get('reasonForLeaving'), i])
    elif section == 'skills':
        set_({'skills': g('skills'), 'languages': g('languages')})
    else:
        raise BadRequestError('unknown section')
    record_field_changes(company_id=company_id, employee_id=employee_id, changes=changes, source=source, ref_id=ref_id,
                         actor_id=actor_id)
    return changes
