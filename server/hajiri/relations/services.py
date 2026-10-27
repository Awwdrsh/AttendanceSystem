"""
Employee relations (port of apps/api/src/modules/relations/service.ts and routes.ts): grievances, disciplinary action,
milestones, acting and deputation, Know Your Employee, sign-in pop-ups and registered clock-in devices.

Grievances are heard by a committee that HR names; they are never routed to the line manager. Free text is encrypted,
and every look at a case by someone other than the person it is about is audited. Exclusions (conflicts of interest)
only grow (an administrator may lift one), and nobody can make themselves the only handler of a case.

One function per TS route. They take the signed-in AuthUser and a camelCase body shaped like the TS one, validate it
with the zod-equivalent schema, raise hajiri.core.errors with the TS messages and return dicts in the TS response shape.

Used by other slices:
- assert_registered_device(company_id, employee_id, token): punch calls it before a browser or app clock-in.
- relations_daily(now=None): the daily job 'relations' (jobs.py).
- relations_policy(company_id), completeness(company_id, employee_id=None), milestones(company_id, from_iso, days).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from django.db import transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import now as _now, npt_today
from hajiri.core.crypto import field_cipher, sha256_hex
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import (MISSING, Arr, Bool, DateTimeStr, Enum, Int, IsoDate, Obj, Str, Uuid, parse, parse_uuid,
                                safe_parse)
from hajiri.notifications.service import notify, user_id_for_employee, user_ids_with_role

DAY = timedelta(days=1)

# ---------------------------------------------------------------- policy

POLICY_SCHEMA = Obj({
    'retirementAge': Int(min=40, max=80, default=58),
    'serviceYears': Arr(Int(min=1, max=60), max=20, default=lambda: [1, 5, 10, 15, 20, 25, 30]),
    'registeredDevicesOnly': Bool(default=False),
    'appealDays': Int(min=1, max=90, default=15),
})


def relations_policy(company_id: str) -> dict:
    r = one("SELECT value FROM company_setting WHERE company_id = %s AND key = 'relations.policy'", [company_id])
    ok, p = safe_parse(POLICY_SCHEMA, (r['value'] if r else None) or {})
    return p if ok else parse(POLICY_SCHEMA, {})


# ---------------------------------------------------------------- helpers

def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def is_hr(u: AuthUser) -> bool:
    return has_role(u, 'hr')


def _require_hr(u: AuthUser) -> None:
    # requireRole('hr') on the TS route.
    if not is_hr(u):
        raise ForbiddenError('requires role hr')


def today() -> str:
    return npt_today().isoformat()


def new_id() -> str:
    return str(one('SELECT uuidv7() AS id')['id'])


def next_number(table: str, company_id: str) -> int:
    """Next per-company case number; the advisory lock keeps two filings from taking the same number."""
    assert table in ('grievance', 'disciplinary_case')
    execute('SELECT pg_advisory_xact_lock(hashtext(%s))', [f'{table}:{company_id}'])
    return int(one(f'SELECT coalesce(max(number), 0) + 1 AS n FROM {table} WHERE company_id = %s', [company_id])['n'])


def enc_text(text: str, aad: str) -> bytes:
    return field_cipher().encrypt(text, aad)


def dec_text(blob, aad: str) -> str | None:
    return field_cipher().decrypt(bytes(blob), aad) if blob is not None else None


def grievance_aad(company_id: str, id_: str, field: str) -> str:
    return f'grievance:{company_id}:{id_}:{field}'


def grievance_note_aad(grievance_id: str, note_id: str) -> str:
    return f'grievance_note:{grievance_id}:{note_id}'


def case_aad(company_id: str, id_: str, field: str) -> str:
    return f'disciplinary:{company_id}:{id_}:{field}'


def _remind_once(company_id: str, kind: str, ref: str) -> bool:
    return execute('INSERT INTO relations_reminder_sent (company_id, kind, ref) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING',
                   [company_id, kind, ref]) > 0


def _text(max_: int, **kw) -> Str:
    """z.string().trim().min(1).max(n)"""
    return Str(trim=True, min=1, max=max_, **kw)


def _defined(**kw) -> dict:
    """An object as JSON.stringify writes it: keys whose value is undefined (MISSING) are left out."""
    return {k: v for k, v in kw.items() if v is not MISSING}


def _iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def _employee_in_company(company_id: str, employee_id: str) -> dict:
    r = one('SELECT id, name_en, emp_code, status FROM employee WHERE id = %s AND company_id = %s', [employee_id, company_id])
    if not r:
        raise NotFoundError('employee not found')
    return r


def _not_linked() -> BadRequestError:
    return BadRequestError('Your account is not linked to an employee record.')


# ================================================================ policy routes

def get_policy(user: AuthUser) -> dict:
    _require_hr(user)
    return {'policy': relations_policy(company_id_of(user))}


def save_policy(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    p = parse(POLICY_SCHEMA, body)
    p['serviceYears'] = sorted(set(p['serviceYears']))
    execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'relations.policy', %s)
               ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value""", [company_id, json.dumps(p)])
    audit('update', 'company_setting', actor_id=user.id, row_id=None, after={'key': 'relations.policy', **p})
    return {'policy': p}


# ================================================================ grievances

GRIEVANCE_CATEGORIES = ('harassment', 'discrimination', 'safety', 'pay_benefits', 'management', 'policy', 'other')
GRIEVANCE_CATEGORY_LABEL = {
    'harassment': 'Harassment or bullying', 'discrimination': 'Discrimination', 'safety': 'Health and safety',
    'pay_benefits': 'Pay and benefits', 'management': 'Treatment by a manager', 'policy': 'Company policy or practice',
    'other': 'Something else',
}
GRIEVANCE_STATUS_LABEL = {
    'submitted': 'Submitted', 'under_review': 'Under review', 'hearing_scheduled': 'Hearing set', 'decided': 'Decided',
    'appealed': 'Appealed', 'closed': 'Closed', 'withdrawn': 'Withdrawn',
}
OUTCOME_LABEL = {'upheld': 'Upheld', 'partly_upheld': 'Partly upheld', 'not_upheld': 'Not upheld'}
OUTCOMES = tuple(OUTCOME_LABEL)
OPEN_GRIEVANCE = ('submitted', 'under_review', 'hearing_scheduled')


def is_committee_member(company_id: str, user_id: str) -> bool:
    return bool(one('SELECT 1 AS x FROM grievance_committee_member WHERE company_id = %s AND user_id = %s', [company_id, user_id]))


def _grievance_row(r: dict) -> dict:
    for k in ('id', 'company_id', 'employee_id', 'user_id', 'decided_by', 'appeal_decided_by'):
        if k in r:
            r[k] = _s(r[k])
    r['excluded_user_ids'] = [str(x) for x in (r.get('excluded_user_ids') or [])]
    return r


def grievance_role(user: AuthUser, g: dict) -> str | None:
    """
    Who may see a grievance: the person who filed it, HR and the grievance committee. Line managers never see a
    grievance through their role. Anyone HR excluded from a case (a conflict of interest) loses access to it.
    """
    if user.company_id != g['company_id']:
        return None
    if g['user_id'] == user.id:
        return 'complainant'
    if user.id in g['excluded_user_ids']:
        return None
    if is_hr(user):
        return 'hr'
    if is_committee_member(g['company_id'], user.id):
        return 'committee'
    return None


def grievance_handlers(company_id: str, user_id: str, excluded_user_ids: list[str]) -> list[str]:
    """HR and the committee, minus anyone excluded from the case and the complainant."""
    hr = user_ids_with_role(company_id, 'hr')
    committee = [str(r['user_id']) for r in rows(
        """SELECT m.user_id FROM grievance_committee_member m JOIN app_user u ON u.id = m.user_id AND u.status = 'active'
            WHERE m.company_id = %s""", [company_id])]
    excluded = {str(x) for x in excluded_user_ids}
    return [i for i in dict.fromkeys([*hr, *committee]) if i != user_id and i not in excluded]


def committee(user: AuthUser) -> dict:
    company_id = company_id_of(user)
    member = is_committee_member(company_id, user.id)
    if not is_hr(user) and not member:
        return {'members': [], 'isMember': False}
    members = rows("""SELECT m.user_id, u.name, u.email, m.added_at FROM grievance_committee_member m JOIN app_user u ON u.id = m.user_id
                       WHERE m.company_id = %s ORDER BY u.name""", [company_id])
    return {'members': [{**m, 'user_id': str(m['user_id'])} for m in members], 'isMember': member}


def set_committee(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = parse(Obj({'userIds': Arr(Uuid(), max=20)}), body)
    ids = list(dict.fromkeys(b['userIds']))
    ok = rows("SELECT id FROM app_user WHERE company_id = %s AND status = 'active' AND id = ANY(%s::uuid[])", [company_id, ids])
    if len(ok) != len(ids):
        raise BadRequestError('every member must be an active user in your company')
    with transaction.atomic():
        execute('DELETE FROM grievance_committee_member WHERE company_id = %s AND NOT (user_id = ANY(%s::uuid[]))', [company_id, ids])
        for i in ids:
            execute('INSERT INTO grievance_committee_member (company_id, user_id, added_by) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING',
                    [company_id, i, user.id])
        audit('update', 'grievance_committee_member', actor_id=user.id, after={'userIds': ids})
    return {'ok': True, 'members': len(ids)}


def company_users(user: AuthUser) -> list[dict]:
    """Active sign-in accounts of the company (auth.users() in the React pages, for the committee and exclusions)."""
    return [{**r, 'id': str(r['id'])} for r in rows(
        "SELECT id, name, email FROM app_user WHERE company_id = %s AND status = 'active' ORDER BY name, email",
        [company_id_of(user)])]


FILE_GRIEVANCE = Obj({
    'category': Enum(GRIEVANCE_CATEGORIES), 'subject': _text(160), 'description': _text(8000),
    'concerns': Str(trim=True, max=500, nullish=True),
    'fileIds': Arr(Uuid(), max=5, default=list),
    'excludeUserIds': Arr(Uuid(), max=10, default=list),
})


def file_grievance(user: AuthUser, body: dict) -> dict:
    from hajiri.files.store import attach_file
    company_id = company_id_of(user)
    if not user.employee_id:
        raise _not_linked()
    b = parse(FILE_GRIEVANCE, body)
    with transaction.atomic():
        id_ = new_id()
        number = next_number('grievance', company_id)
        execute("""INSERT INTO grievance (id, company_id, number, employee_id, user_id, category, subject_enc, description_enc, concerns_enc,
                                          excluded_user_ids)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid[])""",
                [id_, company_id, number, user.employee_id, user.id, b['category'],
                 enc_text(b['subject'], grievance_aad(company_id, id_, 'subject')),
                 enc_text(b['description'], grievance_aad(company_id, id_, 'description')),
                 enc_text(b['concerns'], grievance_aad(company_id, id_, 'concerns')) if b.get('concerns') else None,
                 b['excludeUserIds']])
        for file_id in b['fileIds']:
            attach_file(company_id=company_id, file_id=file_id, user_id=user.id, purpose='grievance_attachment',
                        attached_to=f'grievance:{id_}')
            execute('INSERT INTO grievance_file (grievance_id, file_id) VALUES (%s, %s)', [id_, file_id])
        audit('create', 'grievance', actor_id=user.id, row_id=id_, after={'number': number, 'category': b['category']})
        # The notice says only that a grievance arrived: no names, no subject. People the complainant names (e.g. the
        # person the complaint is about) never see it, not even the arrival notice.
        for uid in grievance_handlers(company_id, user.id, b['excludeUserIds']):
            notify(company_id=company_id, user_id=uid, template='relations.grievance.new', title=f'New grievance G-{number}',
                   body='A confidential grievance was filed. Open Employee relations, Grievances to review it.',
                   payload={'grievanceId': id_})
    return {'id': id_, 'number': number}


def grievance_view(g: dict, role: str) -> dict:
    def d(field: str, col: str):
        return dec_text(g.get(col), grievance_aad(g['company_id'], g['id'], field))
    return {
        'id': g['id'], 'number': g['number'], 'category': g['category'], 'status': g['status'],
        'subject': d('subject', 'subject_enc'), 'description': d('description', 'description_enc'),
        'concerns': d('concerns', 'concerns_enc'), 'employeeId': g['employee_id'], 'employeeName': g.get('employee_name'),
        'empCode': g.get('emp_code'), 'hearingAt': g['hearing_at'], 'hearingPlace': g['hearing_place'], 'outcome': g['outcome'],
        'findings': d('findings', 'findings_enc'), 'decidedAt': g['decided_at'],
        'appealReason': d('appeal_reason', 'appeal_reason_enc'), 'appealedAt': g['appealed_at'],
        'appealOutcome': g['appeal_outcome'], 'appealNote': d('appeal_note', 'appeal_note_enc'),
        'appealDecidedAt': g['appeal_decided_at'], 'createdAt': g['created_at'], 'updatedAt': g['updated_at'], 'role': role,
        'excludedUserIds': [] if role == 'complainant' else g['excluded_user_ids'],
    }


_GRIEVANCE_SELECT = 'SELECT g.*, e.name_en AS employee_name, e.emp_code FROM grievance g JOIN employee e ON e.id = g.employee_id'


def load_grievance(user: AuthUser, id_: str, lock: bool = False) -> tuple[dict, str]:
    r = one(f"{_GRIEVANCE_SELECT} WHERE g.id = %s AND g.company_id = %s {'FOR UPDATE OF g' if lock else ''}",
            [id_, company_id_of(user)])
    g = _grievance_row(r) if r else None
    role = grievance_role(user, g) if g else None
    if not g or not role:
        raise NotFoundError('grievance not found')
    return g, role


GRIEVANCE_QUERY = Obj({'scope': Enum(('mine', 'handle'), default='mine'), 'status': Str(max=40, optional=True)})


def list_grievances(user: AuthUser, query: dict | None = None) -> dict:
    company_id = company_id_of(user)
    q = parse(GRIEVANCE_QUERY, {k: v for k, v in (query or {}).items() if v not in (None, '')})
    if q['scope'] == 'mine':
        rs = rows(f'{_GRIEVANCE_SELECT} WHERE g.company_id = %s AND g.user_id = %s ORDER BY g.created_at DESC', [company_id, user.id])
        return {'grievances': [grievance_view(_grievance_row(g), 'complainant') for g in rs]}
    member = is_committee_member(company_id, user.id)
    if not is_hr(user) and not member:
        raise ForbiddenError('only HR and the grievance committee handle grievances')
    rs = rows(f"""{_GRIEVANCE_SELECT}
                  WHERE g.company_id = %s AND g.user_id <> %s AND NOT (%s::uuid = ANY(g.excluded_user_ids)) AND (%s::text IS NULL OR g.status = %s)
                  ORDER BY (g.status IN ('decided', 'closed', 'withdrawn')), g.created_at DESC LIMIT 500""",
              [company_id, user.id, user.id, q.get('status'), q.get('status')])
    role = 'hr' if is_hr(user) else 'committee'
    # Lists show the subject only; the full text opens (and is audited) per case.
    out = []
    for g in rs:
        g = _grievance_row(g)
        for col in ('description_enc', 'concerns_enc', 'findings_enc', 'appeal_reason_enc', 'appeal_note_enc'):
            g[col] = None
        out.append(grievance_view(g, role))
    return {'grievances': out}


def grievance_detail(user: AuthUser, id_: str) -> dict:
    g, role = load_grievance(user, parse_uuid(id_))
    notes = rows(f"""SELECT n.id, n.author_id, u.name AS author_name, n.shared, n.body_enc, n.created_at FROM grievance_note n
                      JOIN app_user u ON u.id = n.author_id
                     WHERE n.grievance_id = %s {'AND n.shared' if role == 'complainant' else ''} ORDER BY n.created_at""", [g['id']])
    files = rows("""SELECT f.id, f.filename, f.mime, f.bytes FROM grievance_file gf JOIN stored_file f ON f.id = gf.file_id
                     WHERE gf.grievance_id = %s AND f.deleted_at IS NULL""", [g['id']])
    if role != 'complainant':
        audit('view', 'grievance', actor_id=user.id, row_id=g['id'])
    p = relations_policy(g['company_id'])
    appeal_until = g['decided_at'] + timedelta(days=p['appealDays']) if g['decided_at'] else None
    out_notes = []
    for n in notes:
        nid, author = str(n['id']), str(n['author_id'])
        out_notes.append({'id': nid, 'authorName': 'You' if author == g['user_id'] else n['author_name'], 'mine': author == user.id,
                          'fromComplainant': author == g['user_id'], 'shared': n['shared'],
                          'body': dec_text(n['body_enc'], grievance_note_aad(g['id'], nid)), 'createdAt': n['created_at']})
    return {
        'grievance': grievance_view(g, role), 'notes': out_notes,
        'files': [{**f, 'id': str(f['id'])} for f in files],
        'canAppeal': role == 'complainant' and g['status'] == 'decided' and not g['appealed_at'] and bool(appeal_until)
        and appeal_until > _now(),
        'appealUntil': appeal_until,
    }


def grievance_file(user: AuthUser, id_: str, file_id: str) -> dict:
    """The stored file (store.read_file shape) if the user can see the case."""
    from hajiri.files.store import read_file
    g, role = load_grievance(user, parse_uuid(id_))
    file_id = parse_uuid(file_id)
    if not one('SELECT 1 AS x FROM grievance_file WHERE grievance_id = %s AND file_id = %s', [g['id'], file_id]):
        raise NotFoundError('file not found')
    if role != 'complainant':
        audit('view_file', 'grievance', actor_id=user.id, row_id=g['id'], after={'fileId': file_id})
    return read_file(g['company_id'], file_id)


def add_note(user: AuthUser, id_: str, body: dict) -> dict:
    id_ = parse_uuid(id_)
    b = parse(Obj({'body': _text(4000), 'shared': Bool(default=False)}), body)
    with transaction.atomic():
        g, role = load_grievance(user, id_, lock=True)
        if g['status'] in ('closed', 'withdrawn'):
            raise ConflictError(f"this grievance is {g['status']}")
        shared = True if role == 'complainant' else b['shared']
        note_id = new_id()
        execute('INSERT INTO grievance_note (id, grievance_id, author_id, shared, body_enc) VALUES (%s, %s, %s, %s, %s)',
                [note_id, g['id'], user.id, shared, enc_text(b['body'], grievance_note_aad(g['id'], note_id))])
        execute('UPDATE grievance SET updated_at = now() WHERE id = %s', [g['id']])
        if role == 'complainant':
            for uid in grievance_handlers(g['company_id'], g['user_id'], g['excluded_user_ids']):
                notify(company_id=g['company_id'], user_id=uid, template='relations.grievance.note',
                       title=f"G-{g['number']}: new message from the employee", body='Open the grievance to read it.',
                       payload={'grievanceId': g['id']})
        elif shared:
            notify(company_id=g['company_id'], user_id=g['user_id'], employee_id=g['employee_id'], template='relations.grievance.note',
                   title=f"Your grievance G-{g['number']}: new message", body='The grievance committee sent you a message.',
                   payload={'grievanceId': g['id']})
        audit('note', 'grievance', actor_id=user.id, row_id=g['id'], after={'shared': shared})
    return {'id': note_id}


UPDATE_GRIEVANCE = Obj({
    'status': Enum(('under_review', 'hearing_scheduled'), optional=True),
    'hearingAt': DateTimeStr(nullish=True),
    'hearingPlace': Str(trim=True, max=200, nullish=True),
    'excludeUserIds': Arr(Uuid(), max=20, optional=True),
}, strict=True)


def update_grievance(user: AuthUser, id_: str, body: dict) -> dict:
    """Committee or HR: acknowledge, schedule a hearing, or exclude someone with a conflict of interest (HR only)."""
    id_ = parse_uuid(id_)
    b = parse(UPDATE_GRIEVANCE, body)
    exclude = b.get('excludeUserIds', MISSING)
    with transaction.atomic():
        g, role = load_grievance(user, id_, lock=True)
        if role == 'complainant':
            raise ForbiddenError('the committee handles this grievance')
        if g['status'] not in OPEN_GRIEVANCE:
            raise ConflictError(f"this grievance is {g['status']}")
        if exclude is not MISSING and role != 'hr':
            raise ForbiddenError('only HR can exclude people from a case')
        if exclude is not MISSING and user.id in exclude:
            raise BadRequestError('you cannot exclude yourself; ask another HR person')
        if exclude is not MISSING:
            # Exclusions only grow (an admin may lift one), and nobody can make themselves the sole handler of a case.
            removed = [x for x in g['excluded_user_ids'] if x not in exclude]
            if removed and not has_role(user, 'admin'):
                raise ForbiddenError('only an administrator can lift an exclusion')
            others = [x for x in grievance_handlers(g['company_id'], g['user_id'], exclude) if x != user.id]
            if not others:
                raise BadRequestError('at least one other HR or committee member must be able to handle this case')
        status = b.get('status') or g['status']
        hearing_at = b.get('hearingAt', MISSING)
        if hearing_at:
            status = 'hearing_scheduled'
        if status == 'hearing_scheduled' and not (hearing_at or g['hearing_at']):
            raise BadRequestError('set the hearing date and time')
        place = b.get('hearingPlace', MISSING)
        execute("""UPDATE grievance SET status = %s, hearing_at = CASE WHEN %s::boolean THEN %s::timestamptz ELSE hearing_at END,
                          hearing_place = CASE WHEN %s::boolean THEN %s ELSE hearing_place END,
                          excluded_user_ids = coalesce(%s::uuid[], excluded_user_ids), updated_at = now()
                    WHERE id = %s""",
                [status, hearing_at is not MISSING, hearing_at or None, place is not MISSING, None if place is MISSING else place,
                 None if exclude is MISSING else exclude, g['id']])
        audit('update', 'grievance', actor_id=user.id, row_id=g['id'], before={'status': g['status']},
              after=_defined(status=status, hearingAt=hearing_at, excluded=exclude))
        if hearing_at:
            notify(company_id=g['company_id'], user_id=g['user_id'], employee_id=g['employee_id'], template='relations.grievance.hearing',
                   title=f"Hearing set for your grievance G-{g['number']}",
                   body=f"The committee will hear your grievance on {bs_label(hearing_at)}{f' at {place}' if place else ''}.",
                   payload={'grievanceId': g['id']}, channels=('push',))
        elif status == 'under_review' and g['status'] == 'submitted':
            notify(company_id=g['company_id'], user_id=g['user_id'], employee_id=g['employee_id'], template='relations.grievance.review',
                   title=f"Your grievance G-{g['number']} is under review", body='The grievance committee has started reviewing it.',
                   payload={'grievanceId': g['id']})
    return {'ok': True}


def decide_grievance(user: AuthUser, id_: str, body: dict) -> dict:
    id_ = parse_uuid(id_)
    b = parse(Obj({'outcome': Enum(OUTCOMES), 'findings': _text(8000)}), body)
    with transaction.atomic():
        g, role = load_grievance(user, id_, lock=True)
        if role == 'complainant':
            raise ForbiddenError('the committee decides this grievance')
        if g['status'] not in OPEN_GRIEVANCE:
            raise ConflictError(f"this grievance is {g['status']}")
        execute("""UPDATE grievance SET status = 'decided', outcome = %s, findings_enc = %s, decided_by = %s, decided_at = now(),
                          updated_at = now() WHERE id = %s""",
                [b['outcome'], enc_text(b['findings'], grievance_aad(g['company_id'], g['id'], 'findings')), user.id, g['id']])
        audit('decide', 'grievance', actor_id=user.id, row_id=g['id'], after={'outcome': b['outcome']})
        p = relations_policy(g['company_id'])
        notify(company_id=g['company_id'], user_id=g['user_id'], employee_id=g['employee_id'], template='relations.grievance.decided',
               title=f"Decision on your grievance G-{g['number']}",
               body=f"The committee has decided. Read the findings; you can appeal within {p['appealDays']} days.",
               payload={'grievanceId': g['id']}, channels=('push',))
    return {'ok': True}


def appeal_grievance(user: AuthUser, id_: str, body: dict) -> dict:
    id_ = parse_uuid(id_)
    b = parse(Obj({'reason': _text(4000)}), body)
    with transaction.atomic():
        g, role = load_grievance(user, id_, lock=True)
        if role != 'complainant':
            raise ForbiddenError('only the person who filed the grievance can appeal')
        if g['status'] != 'decided':
            raise ConflictError('you can appeal once a decision has been made')
        if g['appealed_at']:
            raise ConflictError('this grievance has already been appealed')
        p = relations_policy(g['company_id'])
        if _now() - g['decided_at'] > timedelta(days=p['appealDays']):
            raise ConflictError(f"the {p['appealDays']}-day appeal window has passed")
        execute("""UPDATE grievance SET status = 'appealed', appeal_reason_enc = %s, appealed_at = now(), updated_at = now()
                    WHERE id = %s""", [enc_text(b['reason'], grievance_aad(g['company_id'], g['id'], 'appeal_reason')), g['id']])
        audit('appeal', 'grievance', actor_id=user.id, row_id=g['id'])
        # Appeals are heard by HR, not by the committee that decided.
        for uid in user_ids_with_role(g['company_id'], 'hr'):
            if uid == g['user_id'] or uid in g['excluded_user_ids']:
                continue
            notify(company_id=g['company_id'], user_id=uid, template='relations.grievance.appeal', title=f"Grievance G-{g['number']} appealed",
                   body='The employee appealed the decision. HR hears appeals.', payload={'grievanceId': g['id']})
    return {'ok': True}


APPEAL_DECISION = Obj({'outcome': Enum(('decision_upheld', 'decision_changed')), 'note': _text(4000),
                       'newOutcome': Enum(OUTCOMES, optional=True)})


def decide_appeal(user: AuthUser, id_: str, body: dict) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    b = parse(APPEAL_DECISION, body)
    with transaction.atomic():
        g, role = load_grievance(user, id_, lock=True)
        if role != 'hr':
            raise ForbiddenError('HR hears appeals')
        if g['status'] != 'appealed':
            raise ConflictError('this grievance has no open appeal')
        if g['decided_by'] == user.id:
            raise ForbiddenError('someone other than the person who decided must hear the appeal')
        if b['outcome'] == 'decision_changed' and not b.get('newOutcome'):
            raise BadRequestError('choose the new outcome')
        execute("""UPDATE grievance SET status = 'closed', appeal_outcome = %s, appeal_note_enc = %s, appeal_decided_by = %s,
                          appeal_decided_at = now(), outcome = coalesce(%s, outcome), updated_at = now() WHERE id = %s""",
                [b['outcome'], enc_text(b['note'], grievance_aad(g['company_id'], g['id'], 'appeal_note')), user.id,
                 b.get('newOutcome') if b['outcome'] == 'decision_changed' else None, g['id']])
        audit('appeal_decide', 'grievance', actor_id=user.id, row_id=g['id'],
              after=_defined(outcome=b['outcome'], newOutcome=b.get('newOutcome', MISSING)))
        notify(company_id=g['company_id'], user_id=g['user_id'], employee_id=g['employee_id'], template='relations.grievance.appeal_decided',
               title=f"Appeal decided: G-{g['number']}",
               body=('The original decision stands. Read the note for the reasons.' if b['outcome'] == 'decision_upheld'
                     else 'The decision was changed on appeal. Read the note for details.'),
               payload={'grievanceId': g['id']}, channels=('push',))
    return {'ok': True}


def withdraw_grievance(user: AuthUser, id_: str) -> dict:
    id_ = parse_uuid(id_)
    with transaction.atomic():
        g, role = load_grievance(user, id_, lock=True)
        if role != 'complainant':
            raise ForbiddenError('only the person who filed it can withdraw a grievance')
        if g['status'] not in OPEN_GRIEVANCE:
            raise ConflictError(f"this grievance is {g['status']}")
        execute("UPDATE grievance SET status = 'withdrawn', updated_at = now() WHERE id = %s", [g['id']])
        audit('withdraw', 'grievance', actor_id=user.id, row_id=g['id'])
        for uid in grievance_handlers(g['company_id'], g['user_id'], g['excluded_user_ids']):
            notify(company_id=g['company_id'], user_id=uid, template='relations.grievance.withdrawn',
                   title=f"Grievance G-{g['number']} withdrawn", body='The employee withdrew the grievance.',
                   payload={'grievanceId': g['id']})
    return {'ok': True}


def close_grievance(user: AuthUser, id_: str) -> dict:
    id_ = parse_uuid(id_)
    with transaction.atomic():
        g, role = load_grievance(user, id_, lock=True)
        if role == 'committee':
            raise ForbiddenError('HR or the employee closes a decided grievance')
        if g['status'] != 'decided':
            raise ConflictError('only a decided grievance can be closed')
        execute("UPDATE grievance SET status = 'closed', updated_at = now() WHERE id = %s", [g['id']])
        audit('close', 'grievance', actor_id=user.id, row_id=g['id'])
    return {'ok': True}


# ================================================================ disciplinary action

DEFAULT_STAGES = [
    {'code': 'verbal_warning', 'name': 'Verbal warning', 'severity': 1, 'effect': 'none', 'letter': None},
    {'code': 'written_warning', 'name': 'Written warning', 'severity': 2, 'effect': 'none', 'letter': 'warning'},
    {'code': 'final_warning', 'name': 'Final written warning', 'severity': 3, 'effect': 'none', 'letter': 'warning'},
    {'code': 'suspension', 'name': 'Suspension', 'severity': 4, 'effect': 'suspension', 'letter': 'warning'},
    {'code': 'termination', 'name': 'Termination', 'severity': 5, 'effect': 'termination', 'letter': None},
]
CASE_CATEGORIES = ('attendance', 'conduct', 'performance', 'policy', 'safety', 'misconduct', 'other')
CASE_CATEGORY_LABEL = {'attendance': 'Attendance', 'conduct': 'Conduct', 'performance': 'Performance', 'policy': 'Policy breach',
                       'safety': 'Safety', 'misconduct': 'Misconduct', 'other': 'Other'}
CASE_STATUS_LABEL = {'open': 'Under investigation', 'show_cause': 'Waiting for response', 'responded': 'Response received',
                     'decided': 'Decided', 'withdrawn': 'Withdrawn'}
OPEN_CASE = ('open', 'show_cause', 'responded')
VISIBLE_TO_EMPLOYEE = ('show_cause', 'responded', 'decided')


def ensure_stages(company_id: str) -> None:
    if one('SELECT 1 AS x FROM disciplinary_stage WHERE company_id = %s LIMIT 1', [company_id]):
        return
    for i, s in enumerate(DEFAULT_STAGES):
        execute("""INSERT INTO disciplinary_stage (company_id, code, name, severity, effect, letter_template_code, sort)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                [company_id, s['code'], s['name'], s['severity'], s['effect'], s['letter'], i])


def list_stages(user: AuthUser) -> dict:
    company_id = company_id_of(user)
    ensure_stages(company_id)
    rs = rows("""SELECT id, code, name, severity, effect, letter_template_code, sort, active FROM disciplinary_stage
                  WHERE company_id = %s ORDER BY sort, severity""", [company_id])
    return {'stages': [{**r, 'id': str(r['id'])} for r in rs]}


SAVE_STAGES = Obj({'stages': Arr(Obj({
    'id': Uuid(optional=True), 'code': Str(regex=r'^[a-z0-9_]{2,40}$'), 'name': _text(80), 'severity': Int(min=1, max=9),
    'effect': Enum(('none', 'suspension', 'termination')), 'letterTemplateCode': Str(trim=True, max=40, nullish=True),
    'active': Bool(default=True),
}), min=1, max=20)})


def save_stages(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = parse(SAVE_STAGES, body)
    ensure_stages(company_id)
    with transaction.atomic():
        for i, s in enumerate(b['stages']):
            if s.get('id'):
                n = execute("""UPDATE disciplinary_stage SET code = %s, name = %s, severity = %s, effect = %s, letter_template_code = %s,
                                      sort = %s, active = %s WHERE id = %s AND company_id = %s""",
                            [s['code'], s['name'], s['severity'], s['effect'], s.get('letterTemplateCode') or None, i, s['active'],
                             s['id'], company_id])
                if not n:
                    raise NotFoundError('stage not found')
            else:
                execute("""INSERT INTO disciplinary_stage (company_id, code, name, severity, effect, letter_template_code, sort, active)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        [company_id, s['code'], s['name'], s['severity'], s['effect'], s.get('letterTemplateCode') or None, i, s['active']])
        audit('update', 'disciplinary_stage', actor_id=user.id, after={'count': len(b['stages'])})
    return {'ok': True}


_CASE_SELECT = """SELECT k.*, k.incident_date::text AS incident_date, k.response_due::text AS response_due,
        k.suspension_from::text AS suspension_from, k.suspension_to::text AS suspension_to, e.name_en AS employee_name, e.emp_code,
        s.name AS stage_name, s.effect AS stage_effect, s.letter_template_code AS stage_letter, l.ref_no AS letter_ref
   FROM disciplinary_case k JOIN employee e ON e.id = k.employee_id LEFT JOIN disciplinary_stage s ON s.id = k.stage_id
   LEFT JOIN issued_letter l ON l.id = k.letter_id"""


def _case_row(k: dict) -> dict:
    for c in ('id', 'company_id', 'employee_id', 'stage_id', 'exit_id', 'letter_id', 'opened_by', 'decided_by'):
        k[c] = _s(k.get(c))
    return k


def case_view(k: dict, for_employee: bool) -> dict:
    def d(field: str, col: str):
        return dec_text(k.get(col), case_aad(k['company_id'], k['id'], field))
    return {
        'id': k['id'], 'number': k['number'], 'employeeId': k['employee_id'], 'employeeName': k['employee_name'],
        'empCode': k['emp_code'], 'category': k['category'], 'incidentDate': k['incident_date'],
        'incident': d('incident', 'incident_enc'), 'status': k['status'], 'showCause': d('show_cause', 'show_cause_enc'),
        'showCauseAt': k['show_cause_at'], 'responseDue': k['response_due'], 'response': d('response', 'response_enc'),
        'respondedAt': k['responded_at'], 'stageId': k['stage_id'], 'stageName': k['stage_name'], 'stageEffect': k['stage_effect'],
        'stageLetter': None if for_employee else k['stage_letter'], 'decisionNote': d('decision', 'decision_note_enc'),
        'suspensionFrom': k['suspension_from'], 'suspensionTo': k['suspension_to'], 'letterId': k['letter_id'],
        'letterRef': k['letter_ref'], 'exitId': None if for_employee else k['exit_id'], 'decidedAt': k['decided_at'],
        'createdAt': k['created_at'], 'updatedAt': k['updated_at'],
    }


def load_case(user: AuthUser, id_: str, lock: bool = False) -> dict:
    r = one(f"{_CASE_SELECT} WHERE k.id = %s AND k.company_id = %s {'FOR UPDATE OF k' if lock else ''}", [id_, company_id_of(user)])
    if not r:
        raise NotFoundError('case not found')
    return _case_row(r)


CASES_QUERY = Obj({'employeeId': Uuid(optional=True), 'status': Str(max=20, optional=True)})


def list_cases(user: AuthUser, query: dict | None = None) -> dict:
    _require_hr(user)
    q = parse(CASES_QUERY, {k: v for k, v in (query or {}).items() if v not in (None, '')})
    rs = rows(f"""{_CASE_SELECT} WHERE k.company_id = %s AND (%s::uuid IS NULL OR k.employee_id = %s::uuid) AND (%s::text IS NULL OR k.status = %s)
                  ORDER BY (k.status IN ('decided', 'withdrawn')), k.created_at DESC LIMIT 500""",
              [company_id_of(user), q.get('employeeId'), q.get('employeeId'), q.get('status'), q.get('status')])
    return {'cases': [case_view(_case_row(k), False) for k in rs]}


def my_cases(user: AuthUser) -> dict:
    """The employee's own cases, once HR has asked them to explain or has decided; investigations in progress stay with HR."""
    if not user.employee_id:
        return {'cases': []}
    rs = rows(f"""{_CASE_SELECT} WHERE k.company_id = %s AND k.employee_id = %s AND k.status IN ('show_cause', 'responded', 'decided')
                  ORDER BY k.created_at DESC""", [company_id_of(user), user.employee_id])
    return {'cases': [case_view(_case_row(k), True) for k in rs]}


def case_detail(user: AuthUser, id_: str) -> dict:
    k = load_case(user, parse_uuid(id_))
    own = user.employee_id == k['employee_id']
    if not is_hr(user) and not (own and k['status'] in VISIBLE_TO_EMPLOYEE):
        raise NotFoundError('case not found')
    if not own:
        audit('view', 'disciplinary_case', actor_id=user.id, row_id=k['id'])
    return {'case': case_view(k, own and not is_hr(user))}


OPEN_CASE_BODY = Obj({'employeeId': Uuid(), 'category': Enum(CASE_CATEGORIES), 'incidentDate': IsoDate(), 'incident': _text(8000)})


def open_case(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = parse(OPEN_CASE_BODY, body)
    emp = _employee_in_company(company_id, b['employeeId'])
    if emp['status'] != 'active':
        raise ConflictError('this employee has left')
    if b['incidentDate'] > today():
        raise BadRequestError('the incident date is in the future')
    if user.employee_id == b['employeeId']:
        raise ForbiddenError('you cannot open a case about yourself')
    with transaction.atomic():
        id_ = new_id()
        number = next_number('disciplinary_case', company_id)
        execute("""INSERT INTO disciplinary_case (id, company_id, number, employee_id, category, incident_date, incident_enc, opened_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                [id_, company_id, number, b['employeeId'], b['category'], b['incidentDate'],
                 enc_text(b['incident'], case_aad(company_id, id_, 'incident')), user.id])
        audit('create', 'disciplinary_case', actor_id=user.id, row_id=id_,
              after={'employeeId': b['employeeId'], 'category': b['category'], 'number': number})
    return {'id': id_, 'number': number}


def show_cause(user: AuthUser, id_: str, body: dict) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    b = parse(Obj({'text': _text(8000), 'responseDue': IsoDate()}), body)
    if b['responseDue'] < today():
        raise BadRequestError('the response deadline has already passed')
    with transaction.atomic():
        k = load_case(user, id_, lock=True)
        if k['status'] != 'open':
            raise ConflictError('a show-cause notice was already sent' if k['status'] == 'show_cause' else f"this case is {k['status']}")
        execute("""UPDATE disciplinary_case SET status = 'show_cause', show_cause_enc = %s, show_cause_at = now(), response_due = %s,
                          updated_at = now() WHERE id = %s""",
                [enc_text(b['text'], case_aad(k['company_id'], k['id'], 'show_cause')), b['responseDue'], k['id']])
        audit('show_cause', 'disciplinary_case', actor_id=user.id, row_id=k['id'], after={'responseDue': b['responseDue']})
        uid = user_id_for_employee(k['employee_id'])
        if uid:
            notify(company_id=k['company_id'], user_id=uid, employee_id=k['employee_id'], template='relations.disciplinary.show_cause',
                   title='Please respond to a notice from HR',
                   body=f"HR has asked you to explain an incident. Respond by {bs_label(b['responseDue'])} under Profile, Conduct.",
                   payload={'caseId': k['id']}, channels=('push',))
    return {'ok': True}


def respond_case(user: AuthUser, id_: str, body: dict) -> dict:
    id_ = parse_uuid(id_)
    b = parse(Obj({'response': _text(8000)}), body)
    with transaction.atomic():
        k = load_case(user, id_, lock=True)
        if user.employee_id != k['employee_id']:
            raise NotFoundError('case not found')
        if k['status'] != 'show_cause':
            raise ConflictError('you have already responded' if k['status'] == 'responded' else 'this case is not waiting for your response')
        execute("""UPDATE disciplinary_case SET status = 'responded', response_enc = %s, responded_at = now(), updated_at = now()
                    WHERE id = %s""", [enc_text(b['response'], case_aad(k['company_id'], k['id'], 'response')), k['id']])
        audit('respond', 'disciplinary_case', actor_id=user.id, row_id=k['id'],
              after={'late': bool(k['response_due']) and k['response_due'] < today()})
        for uid in user_ids_with_role(k['company_id'], 'hr'):
            if uid == user.id:
                continue
            notify(company_id=k['company_id'], user_id=uid, template='relations.disciplinary.responded',
                   title=f"Case D-{k['number']}: response received", body=f"{k['employee_name']} responded to the show-cause notice.",
                   payload={'caseId': k['id']})
    return {'ok': True}


DECIDE_CASE = Obj({
    'stageId': Uuid(nullable=True), 'note': _text(8000), 'suspensionFrom': IsoDate(optional=True),
    'suspensionTo': IsoDate(optional=True), 'lastWorkingDay': IsoDate(optional=True),
    # Decide before the response deadline (e.g. the employee admitted the incident in a hearing).
    'force': Bool(default=False),
})


def log_suspension(*, company_id: str, employee_id: str, from_: str, to: str, stage_name: str, actor_id: str) -> None:
    """A suspension decided in a disciplinary case goes on the career timeline (it changes nothing permanent)."""
    from hajiri.people.career import log_career_event
    log_career_event(company_id=company_id, employee_id=employee_id, kind='suspension', effective_date=from_,
                     details={'endDate': to, 'reason': stage_name}, note='Recorded from a disciplinary case', actor_id=actor_id)


def decide_case(user: AuthUser, id_: str, body: dict) -> dict:
    from hajiri.lifecycle.service import record_exit
    _require_hr(user)
    id_ = parse_uuid(id_)
    b = parse(DECIDE_CASE, body)
    with transaction.atomic():
        k = load_case(user, id_, lock=True)
        if k['status'] not in OPEN_CASE:
            raise ConflictError(f"this case is {k['status']}")
        if user.employee_id == k['employee_id']:
            raise ForbiddenError('you cannot decide a case about yourself')
        if k['status'] == 'show_cause' and k['response_due'] and k['response_due'] >= today() and not b['force']:
            raise ConflictError(f"the employee has until {bs_label(k['response_due'])} to respond; decide after that, or confirm you want to decide now")
        stage = None
        if b['stageId']:
            stage = one('SELECT id, name, effect FROM disciplinary_stage WHERE id = %s AND company_id = %s AND active',
                        [b['stageId'], k['company_id']])
            if not stage:
                raise BadRequestError('choose an active stage')
            stage['id'] = str(stage['id'])
        exit_id = None
        effect = stage['effect'] if stage else None
        if effect == 'suspension':
            if not b.get('suspensionFrom') or not b.get('suspensionTo'):
                raise BadRequestError('enter the suspension dates')
            if b['suspensionTo'] < b['suspensionFrom']:
                raise BadRequestError('the suspension ends before it starts')
            log_suspension(company_id=k['company_id'], employee_id=k['employee_id'], from_=b['suspensionFrom'], to=b['suspensionTo'],
                           stage_name=stage['name'], actor_id=user.id)
        if effect == 'termination':
            if not b.get('lastWorkingDay'):
                raise BadRequestError('enter the last working day')
            exit_id = record_exit(company_id=k['company_id'], employee_id=k['employee_id'], kind='termination', voluntary=False,
                                  reason_category='disciplinary', reason=f"Disciplinary case D-{k['number']}",
                                  last_working_day=b['lastWorkingDay'], actor_id=user.id)
        execute("""UPDATE disciplinary_case SET status = 'decided', stage_id = %s, decision_note_enc = %s, suspension_from = %s,
                          suspension_to = %s, exit_id = %s, decided_by = %s, decided_at = now(), updated_at = now() WHERE id = %s""",
                [stage['id'] if stage else None, enc_text(b['note'], case_aad(k['company_id'], k['id'], 'decision')),
                 b.get('suspensionFrom') if effect == 'suspension' else None, b.get('suspensionTo') if effect == 'suspension' else None,
                 exit_id, user.id, k['id']])
        audit('decide', 'disciplinary_case', actor_id=user.id, row_id=k['id'],
              after={'stage': stage['name'] if stage else 'no action', 'exitId': exit_id})
        uid = user_id_for_employee(k['employee_id'])
        if uid:
            notify(company_id=k['company_id'], user_id=uid, employee_id=k['employee_id'], template='relations.disciplinary.decided',
                   title='Outcome of a disciplinary case',
                   body=(f"HR decided: {stage['name']}. See Profile, Conduct for details." if stage
                         else 'HR closed the case with no action. See Profile, Conduct.'),
                   payload={'caseId': k['id']}, channels=('push',))
    return {'ok': True}


def link_letter(user: AuthUser, id_: str, body: dict) -> dict:
    """Link a letter issued under Documents (e.g. a warning letter) to the case."""
    _require_hr(user)
    id_ = parse_uuid(id_)
    b = parse(Obj({'letterId': Uuid()}), body)
    with transaction.atomic():
        k = load_case(user, id_, lock=True)
        letter = one('SELECT employee_id FROM issued_letter WHERE id = %s AND company_id = %s', [b['letterId'], k['company_id']])
        if not letter or str(letter['employee_id']) != k['employee_id']:
            raise BadRequestError('that letter was not issued to this employee')
        execute('UPDATE disciplinary_case SET letter_id = %s, updated_at = now() WHERE id = %s', [b['letterId'], k['id']])
        audit('link_letter', 'disciplinary_case', actor_id=user.id, row_id=k['id'], after={'letterId': b['letterId']})
    return {'ok': True}


def withdraw_case(user: AuthUser, id_: str, body: dict) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    b = parse(Obj({'reason': _text(1000)}), body)
    with transaction.atomic():
        k = load_case(user, id_, lock=True)
        if k['status'] not in OPEN_CASE:
            raise ConflictError(f"this case is {k['status']}")
        execute("""UPDATE disciplinary_case SET status = 'withdrawn', decision_note_enc = %s, decided_by = %s, decided_at = now(),
                          updated_at = now() WHERE id = %s""",
                [enc_text(b['reason'], case_aad(k['company_id'], k['id'], 'decision')), user.id, k['id']])
        audit('withdraw', 'disciplinary_case', actor_id=user.id, row_id=k['id'])
        if k['status'] != 'open':
            uid = user_id_for_employee(k['employee_id'])
            if uid:
                notify(company_id=k['company_id'], user_id=uid, employee_id=k['employee_id'], template='relations.disciplinary.withdrawn',
                       title='A disciplinary case was withdrawn', body='HR withdrew the case. No action will be taken.',
                       payload={'caseId': k['id']})
    return {'ok': True}


# ================================================================ completeness (KYE)

KYE_SECTIONS = ('personal', 'contact', 'emergency', 'family', 'education', 'experience', 'ids', 'bank', 'photo')
SECTION_LABEL = {
    'personal': 'Personal details', 'contact': 'Contact and address', 'emergency': 'Emergency contacts',
    'family': 'Family and nominees', 'education': 'Education', 'experience': 'Previous employment', 'ids': 'ID documents',
    'bank': 'Bank account', 'photo': 'Profile photo',
}
# Sections whose absence counts against the record; previous employment is optional (first jobs have none).
REQUIRED_SECTIONS = ('personal', 'contact', 'emergency', 'family', 'education', 'ids', 'bank', 'photo')


def completeness(company_id: str, employee_id: str | None = None) -> list[dict]:
    rs = rows("""
        SELECT e.id, e.emp_code, e.name_en,
               (e.dob IS NOT NULL AND e.gender IS NOT NULL AND e.marital_status IS NOT NULL AND e.blood_group IS NOT NULL) AS personal,
               (coalesce(e.personal_phone, e.phone) IS NOT NULL AND e.permanent_address IS NOT NULL) AS contact,
               EXISTS (SELECT 1 FROM employee_emergency_contact x WHERE x.employee_id = e.id) AS emergency,
               EXISTS (SELECT 1 FROM employee_family x WHERE x.employee_id = e.id) AS family,
               EXISTS (SELECT 1 FROM employee_education x WHERE x.employee_id = e.id) AS education,
               EXISTS (SELECT 1 FROM employee_experience x WHERE x.employee_id = e.id) AS experience,
               (e.citizenship_no_enc IS NOT NULL AND e.pan_enc IS NOT NULL) AS ids,
               (e.bank_account_enc IS NOT NULL) AS bank,
               (e.avatar_file_id IS NOT NULL) AS photo
          FROM employee e WHERE e.company_id = %s AND e.status = 'active' AND (%s::uuid IS NULL OR e.id = %s::uuid) ORDER BY e.emp_code""",
              [company_id, employee_id, employee_id])
    out = []
    for x in rs:
        missing = [s for s in REQUIRED_SECTIONS if not x[s]]
        pct = (len(REQUIRED_SECTIONS) - len(missing)) / len(REQUIRED_SECTIONS) * 100
        out.append({'employeeId': str(x['id']), 'empCode': x['emp_code'], 'name': x['name_en'], 'missing': missing,
                    'percent': int(pct + 0.5)})  # Math.round
    return out


def completeness_report(user: AuthUser, query: dict | None = None) -> dict:
    _require_hr(user)
    q = parse(Obj({'employeeId': Uuid(optional=True)}), {k: v for k, v in (query or {}).items() if v not in (None, '')})
    return {'employees': completeness(company_id_of(user), q.get('employeeId')), 'sectionLabels': SECTION_LABEL}


# ================================================================ milestones

MILESTONE_KIND = {
    'service': ('Service', 'ok'), 'contract_end': ('Contract end', 'warn'), 'retirement': ('Retirement', 'accent'),
    'probation_end': ('Probation', 'info'), 'assignment_end': ('Temporary role', 'info'), 'exit': ('Leaving', 'bad'),
}


def milestones(company_id: str, from_: str, days: int) -> list[dict]:
    """Upcoming dates HR plans around, from `from_` for `days` days (retirements already due are included)."""
    p = relations_policy(company_id)
    out: list[dict] = []
    to = (date.fromisoformat(from_) + timedelta(days=days)).isoformat()
    for s in rows("""
        SELECT e.id, e.emp_code, e.name_en, (e.join_date + make_interval(years => y))::date::text AS d, y AS years
          FROM employee e CROSS JOIN unnest(%s::int[]) AS y
         WHERE e.company_id = %s AND e.status = 'active' AND e.join_date IS NOT NULL
           AND (e.join_date + make_interval(years => y))::date BETWEEN %s::date AND %s::date""",
                  [p['serviceYears'], company_id, from_, to]):
        out.append({'kind': 'service', 'employeeId': str(s['id']), 'empCode': s['emp_code'], 'name': s['name_en'], 'date': s['d'],
                    'detail': f"{s['years']} {'year' if s['years'] == 1 else 'years'} of service"})
    age = p['retirementAge']
    for e in rows("""
        SELECT e.id, e.emp_code, e.name_en, e.contract_end::text AS contract_end,
               (e.dob + make_interval(years => %(age)s))::date::text AS retire,
               CASE WHEN e.confirmed_on IS NULL THEN coalesce(a.probation_end, e.probation_end)::text END AS probation,
               e.exit_date::text AS exit_date
          FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> %(from)s::date
         WHERE e.company_id = %(co)s AND e.status = 'active'
           AND (e.contract_end BETWEEN %(from)s::date AND %(to)s::date OR (e.dob + make_interval(years => %(age)s))::date <= %(to)s::date
                OR (e.confirmed_on IS NULL AND coalesce(a.probation_end, e.probation_end) BETWEEN %(from)s::date AND %(to)s::date)
                OR e.exit_date BETWEEN %(from)s::date AND %(to)s::date)""",
                  {'co': company_id, 'from': from_, 'to': to, 'age': age}):
        base = {'employeeId': str(e['id']), 'empCode': e['emp_code'], 'name': e['name_en']}
        if e['contract_end'] and from_ <= e['contract_end'] <= to:
            out.append({**base, 'kind': 'contract_end', 'date': e['contract_end'], 'detail': 'Contract ends'})
        if e['retire'] and e['retire'] <= to and not e['exit_date']:
            out.append({**base, 'kind': 'retirement', 'date': e['retire'],
                        'detail': f'Past retirement age ({age})' if e['retire'] < from_ else f'Reaches retirement age ({age})'})
        if e['probation'] and from_ <= e['probation'] <= to:
            out.append({**base, 'kind': 'probation_end', 'date': e['probation'], 'detail': 'Probation ends'})
        if e['exit_date'] and from_ <= e['exit_date'] <= to:
            out.append({**base, 'kind': 'exit', 'date': e['exit_date'], 'detail': 'Last working day'})
    for t in rows("""
        SELECT t.employee_id, e.emp_code, e.name_en, t.end_date::text AS end_date, t.kind FROM employee_temp_assignment t
          JOIN employee e ON e.id = t.employee_id
         WHERE t.company_id = %s AND t.status = 'active' AND t.end_date BETWEEN %s::date AND %s::date""", [company_id, from_, to]):
        out.append({'kind': 'assignment_end', 'employeeId': str(t['employee_id']), 'empCode': t['emp_code'], 'name': t['name_en'],
                    'date': t['end_date'], 'detail': 'Acting assignment ends' if t['kind'] == 'acting' else 'Deputation ends'})
    out.sort(key=lambda m: (m['date'], m['name'].casefold()))
    return out


def list_milestones(user: AuthUser, query: dict | None = None) -> dict:
    _require_hr(user)
    q = parse(Obj({'days': Int(min=1, max=366, coerce=True, default=60)}),
              {k: v for k, v in (query or {}).items() if v not in (None, '')})
    company_id = company_id_of(user)
    return {'milestones': milestones(company_id, today(), q['days']), 'policy': relations_policy(company_id)}


def separating(user: AuthUser) -> dict:
    """Employees with an exit date ahead, plus contract ends and retirements due in the next 180 days."""
    _require_hr(user)
    lst = milestones(company_id_of(user), today(), 180)
    return {'people': [m for m in lst if m['kind'] in ('exit', 'contract_end', 'retirement')]}


def get_contract(user: AuthUser, employee_id: str) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    eid = parse_uuid(employee_id)
    r = one('SELECT contract_end::text AS contract_end, dob::text AS dob FROM employee WHERE id = %s AND company_id = %s', [eid, company_id])
    if not r:
        raise NotFoundError('employee not found')
    p = relations_policy(company_id)
    dob = r['dob']
    retirement = f"{int(dob[:4]) + p['retirementAge']}{dob[4:]}" if dob else None
    return {'contractEnd': r['contract_end'], 'retirementDate': retirement, 'retirementAge': p['retirementAge']}


def set_contract(user: AuthUser, employee_id: str, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    eid = parse_uuid(employee_id)
    b = parse(Obj({'contractEnd': IsoDate(nullable=True)}), body)
    before = one('SELECT contract_end::text AS contract_end, join_date::text AS join_date FROM employee WHERE id = %s AND company_id = %s',
                 [eid, company_id])
    if not before:
        raise NotFoundError('employee not found')
    if b['contractEnd'] and before['join_date'] and b['contractEnd'] < before['join_date']:
        raise BadRequestError('the contract cannot end before the join date')
    with transaction.atomic():
        execute('UPDATE employee SET contract_end = %s, updated_at = now() WHERE id = %s', [b['contractEnd'], eid])
        audit('update', 'employee', actor_id=user.id, row_id=eid, before={'contractEnd': before['contract_end']},
              after={'contractEnd': b['contractEnd']})
    return {'ok': True}


# ================================================================ acting and deputation

_ASSIGNMENT_SELECT = """SELECT t.id, t.employee_id, e.name_en AS employee_name, e.emp_code, t.kind, t.designation, t.designation_id,
        t.branch_id, b.name_en AS branch_name, t.org_unit_id, o.name_en AS unit_name, t.start_date::text AS start_date,
        t.end_date::text AS end_date, t.note, t.status, t.created_at
   FROM employee_temp_assignment t JOIN employee e ON e.id = t.employee_id LEFT JOIN branch b ON b.id = t.branch_id
   LEFT JOIN org_unit o ON o.id = t.org_unit_id"""


def _assignment_row(r: dict) -> dict:
    for c in ('id', 'employee_id', 'designation_id', 'branch_id', 'org_unit_id'):
        r[c] = _s(r[c])
    return r


def assignment_where(a: dict) -> str:
    if a['kind'] == 'acting':
        return a.get('designation') or ''
    return ', '.join(x for x in (a.get('branch_name'), a.get('unit_name')) if x)


ASSIGNMENTS_QUERY = Obj({'employeeId': Uuid(optional=True), 'status': Enum(('active', 'ended', 'cancelled', 'all'), default='active')})


def list_assignments(user: AuthUser, query: dict | None = None) -> dict:
    company_id = company_id_of(user)
    q = parse(ASSIGNMENTS_QUERY, {k: v for k, v in (query or {}).items() if v not in (None, '')})
    # HR sees everyone; anyone else only their own (managers see their team's on the directory card).
    if not is_hr(user) and (not q.get('employeeId') or q['employeeId'] != user.employee_id):
        raise ForbiddenError('HR manages acting and deputation assignments')
    rs = rows(f"""{_ASSIGNMENT_SELECT} WHERE t.company_id = %s AND (%s::uuid IS NULL OR t.employee_id = %s::uuid)
                  AND (%s = 'all' OR t.status = %s) ORDER BY t.start_date DESC LIMIT 500""",
              [company_id, q.get('employeeId'), q.get('employeeId'), q['status'], q['status']])
    return {'assignments': [_assignment_row(r) for r in rs]}


ADD_ASSIGNMENT = Obj({
    'employeeId': Uuid(), 'kind': Enum(('acting', 'deputation')), 'designationId': Uuid(nullish=True),
    'designation': Str(trim=True, max=120, nullish=True), 'branchId': Uuid(nullish=True), 'orgUnitId': Uuid(nullish=True),
    'startDate': IsoDate(), 'endDate': IsoDate(), 'note': Str(trim=True, max=500, nullish=True),
})


def add_assignment(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = parse(ADD_ASSIGNMENT, body)
    if b['endDate'] < b['startDate']:
        raise BadRequestError('the end date is before the start date')
    emp = _employee_in_company(company_id, b['employeeId'])
    if emp['status'] != 'active':
        raise ConflictError('this employee has left')
    designation = b.get('designation') or None
    if b.get('designationId'):
        d = one('SELECT name_en FROM designation WHERE id = %s AND company_id = %s', [b['designationId'], company_id])
        if not d:
            raise BadRequestError('designation does not exist in your company')
        designation = d['name_en']
    for table, ref, label in (('branch', b.get('branchId'), 'branch'), ('org_unit', b.get('orgUnitId'), 'department')):
        if ref and not one(f'SELECT 1 AS x FROM {table} WHERE id = %s AND company_id = %s', [ref, company_id]):
            raise BadRequestError(f'{label} does not exist in your company')
    if b['kind'] == 'acting' and not designation:
        raise BadRequestError('choose the position they are acting in')
    if b['kind'] == 'deputation' and not b.get('branchId') and not b.get('orgUnitId'):
        raise BadRequestError('choose where they are deputed to')
    from hajiri.people.career import log_career_event
    with transaction.atomic():
        if one("""SELECT 1 AS x FROM employee_temp_assignment WHERE employee_id = %s AND kind = %s AND status = 'active'
                   AND daterange(start_date, end_date, '[]') && daterange(%s::date, %s::date, '[]')""",
               [b['employeeId'], b['kind'], b['startDate'], b['endDate']]):
            raise ConflictError(f"an {'acting assignment' if b['kind'] == 'acting' else 'deputation'} already covers part of these dates")
        ins = one("""INSERT INTO employee_temp_assignment (company_id, employee_id, kind, designation, designation_id, branch_id, org_unit_id,
                                                           start_date, end_date, note, created_by)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                  [company_id, b['employeeId'], b['kind'], designation, b.get('designationId') or None, b.get('branchId') or None,
                   b.get('orgUnitId') or None, b['startDate'], b['endDate'], b.get('note') or None, user.id])
        aid = str(ins['id'])
        details = {'endDate': b['endDate']}
        if designation:
            details['designation'] = designation
        if b.get('branchId'):
            details['branchId'] = b['branchId']
        if b.get('orgUnitId'):
            details['orgUnitId'] = b['orgUnitId']
        log_career_event(company_id=company_id, employee_id=b['employeeId'], kind=b['kind'], effective_date=b['startDate'],
                         details=details, note=b.get('note') or None, actor_id=user.id)
        audit('create', 'employee_temp_assignment', actor_id=user.id, row_id=aid,
              after={'kind': b['kind'], 'employeeId': b['employeeId'], 'startDate': b['startDate'], 'endDate': b['endDate']})
        uid = user_id_for_employee(b['employeeId'])
        if uid:
            notify(company_id=company_id, user_id=uid, employee_id=b['employeeId'], template='relations.assignment.new',
                   title=f'Acting as {designation}' if b['kind'] == 'acting' else 'Deputation',
                   body=f"From {bs_label(b['startDate'])} to {bs_label(b['endDate'])}.", payload={'assignmentId': aid})
    return {'id': aid}


def end_assignment(user: AuthUser, id_: str, body: dict) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    b = parse(Obj({'endDate': IsoDate(optional=True), 'cancel': Bool(default=False)}), body or {})
    company_id = company_id_of(user)
    row = one('SELECT status, start_date::text AS start_date, end_date::text AS end_date FROM employee_temp_assignment WHERE id = %s AND company_id = %s',
              [id_, company_id])
    if not row:
        raise NotFoundError('assignment not found')
    if row['status'] != 'active':
        raise ConflictError(f"this assignment is {row['status']}")
    with transaction.atomic():
        if b['cancel']:
            execute("UPDATE employee_temp_assignment SET status = 'cancelled', ended_at = now() WHERE id = %s", [id_])
        else:
            end = b.get('endDate') or today()
            if end < row['start_date']:
                raise BadRequestError('the end date is before the start date; cancel it instead')
            t = today()
            execute("""UPDATE employee_temp_assignment SET end_date = %s, status = CASE WHEN %s::date < %s::date THEN 'ended' ELSE status END,
                              ended_at = CASE WHEN %s::date < %s::date THEN now() END WHERE id = %s""", [end, end, t, end, t, id_])
        audit('cancel' if b['cancel'] else 'end', 'employee_temp_assignment', actor_id=user.id, row_id=id_,
              after=_defined(endDate=b.get('endDate', MISSING)))
    return {'ok': True}


# ================================================================ Know Your Employee

def _audience_employees(company_id: str, kind: str, audience_id: str | None) -> list[str]:
    return [str(r['id']) for r in rows("""
        WITH RECURSIVE units AS (SELECT id FROM org_unit WHERE id = %(aud)s::uuid UNION SELECT o.id FROM org_unit o JOIN units u ON o.parent_id = u.id)
        SELECT e.id FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
         WHERE e.company_id = %(co)s AND e.status = 'active'
           AND (%(kind)s = 'all' OR (%(kind)s = 'branch' AND a.branch_id = %(aud)s::uuid)
                OR (%(kind)s = 'department' AND a.org_unit_id IN (SELECT id FROM units)))""",
        {'co': company_id, 'kind': kind, 'aud': audience_id})]


def list_campaigns(user: AuthUser) -> dict:
    _require_hr(user)
    rs = rows("""
        SELECT c.id, c.title, c.message, c.sections, c.audience_kind, c.audience_id, c.due_date::text AS due_date, c.status, c.created_at,
               c.closed_at, count(r.*)::int AS total, count(r.*) FILTER (WHERE r.status = 'confirmed')::int AS confirmed,
               count(r.*) FILTER (WHERE r.status = 'changes_requested')::int AS changes_requested
          FROM kye_campaign c LEFT JOIN kye_response r ON r.campaign_id = c.id WHERE c.company_id = %s GROUP BY c.id ORDER BY c.created_at DESC""",
              [company_id_of(user)])
    return {'campaigns': [{**r, 'id': str(r['id']), 'audience_id': _s(r['audience_id'])} for r in rs]}


LAUNCH_CAMPAIGN = Obj({
    'title': _text(160), 'message': Str(trim=True, max=2000, nullish=True), 'sections': Arr(Enum(KYE_SECTIONS), min=1),
    'audienceKind': Enum(('all', 'branch', 'department'), default='all'), 'audienceId': Uuid(nullish=True), 'dueDate': IsoDate(),
})


def launch_campaign(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = parse(LAUNCH_CAMPAIGN, body)
    if b['dueDate'] < today():
        raise BadRequestError('the due date has already passed')
    if b['audienceKind'] != 'all':
        table = 'branch' if b['audienceKind'] == 'branch' else 'org_unit'
        if not b.get('audienceId') or not one(f'SELECT 1 AS x FROM {table} WHERE id = %s AND company_id = %s', [b['audienceId'], company_id]):
            raise BadRequestError(f"choose the {b['audienceKind']}")
    audience_id = None if b['audienceKind'] == 'all' else b.get('audienceId')
    with transaction.atomic():
        ins = one("""INSERT INTO kye_campaign (company_id, title, message, sections, audience_kind, audience_id, due_date, created_by)
                     VALUES (%s, %s, %s, %s::text[], %s, %s, %s, %s) RETURNING id""",
                  [company_id, b['title'], b.get('message') or None, list(dict.fromkeys(b['sections'])), b['audienceKind'], audience_id,
                   b['dueDate'], user.id])
        cid = str(ins['id'])
        emps = _audience_employees(company_id, b['audienceKind'], audience_id)
        for e in emps:
            execute('INSERT INTO kye_response (campaign_id, employee_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', [cid, e])
            uid = user_id_for_employee(e)
            if uid:
                notify(company_id=company_id, user_id=uid, employee_id=e, template='relations.kye.new',
                       title=f"Please review your profile: {b['title']}", body=f"Check your details and confirm by {bs_label(b['dueDate'])}.",
                       payload={'campaignId': cid})
        audit('create', 'kye_campaign', actor_id=user.id, row_id=cid, after={'title': b['title'], 'employees': len(emps)})
    return {'id': cid, 'employees': len(emps)}


def campaign_detail(user: AuthUser, id_: str) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    company_id = company_id_of(user)
    camp = one("""SELECT id, title, message, sections, audience_kind, due_date::text AS due_date, status FROM kye_campaign
                   WHERE id = %s AND company_id = %s""", [id_, company_id])
    if not camp:
        raise NotFoundError('campaign not found')
    camp['id'] = str(camp['id'])
    resp = rows("""SELECT r.employee_id, e.name_en, e.emp_code, r.status, r.note, r.responded_at, r.last_nudged_at FROM kye_response r
                    JOIN employee e ON e.id = r.employee_id WHERE r.campaign_id = %s ORDER BY (r.status = 'pending') DESC, e.name_en""", [id_])
    comp = {c['employeeId']: c for c in completeness(company_id)}
    out = []
    for x in resp:
        eid = str(x['employee_id'])
        c = comp.get(eid)
        out.append({**x, 'employee_id': eid, 'completeness': c['percent'] if c else None, 'missing': c['missing'] if c else []})
    return {'campaign': camp, 'responses': out}


def nudge_campaign(user: AuthUser, id_: str) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    company_id = company_id_of(user)
    camp = one('SELECT title, due_date::text AS due_date, status FROM kye_campaign WHERE id = %s AND company_id = %s', [id_, company_id])
    if not camp:
        raise NotFoundError('campaign not found')
    if camp['status'] != 'open':
        raise ConflictError('this campaign is closed')
    sent = 0
    with transaction.atomic():
        # At most one nudge a day per person.
        pending = rows("""UPDATE kye_response SET last_nudged_at = now() WHERE campaign_id = %s AND status = 'pending'
                           AND (last_nudged_at IS NULL OR last_nudged_at < now() - interval '20 hours') RETURNING employee_id""", [id_])
        for p in pending:
            eid = str(p['employee_id'])
            uid = user_id_for_employee(eid)
            if uid:
                notify(company_id=company_id, user_id=uid, employee_id=eid, template='relations.kye.nudge', title=f"Reminder: {camp['title']}",
                       body=f"Please review and confirm your profile by {bs_label(camp['due_date'])}.", payload={'campaignId': id_})
                sent += 1
    return {'nudged': sent}


def close_campaign(user: AuthUser, id_: str) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    with transaction.atomic():
        n = execute("UPDATE kye_campaign SET status = 'closed', closed_at = now() WHERE id = %s AND company_id = %s AND status = 'open'",
                    [id_, company_id_of(user)])
        if not n:
            raise NotFoundError('open campaign not found')
        audit('close', 'kye_campaign', actor_id=user.id, row_id=id_)
    return {'ok': True}


def my_kye(user: AuthUser) -> dict:
    """The employee's open review requests and how complete their record is."""
    if not user.employee_id:
        return {'campaigns': [], 'completeness': None}
    company_id = company_id_of(user)
    rs = rows("""SELECT c.id, c.title, c.message, c.sections, c.due_date::text AS due_date, r.status, r.responded_at FROM kye_response r
                  JOIN kye_campaign c ON c.id = r.campaign_id
                 WHERE r.employee_id = %s AND c.company_id = %s AND c.status = 'open' ORDER BY c.due_date""", [user.employee_id, company_id])
    comp = completeness(company_id, user.employee_id)
    return {'campaigns': [{**r, 'id': str(r['id'])} for r in rs], 'completeness': comp[0] if comp else None,
            'sectionLabels': SECTION_LABEL}


def respond_kye(user: AuthUser, id_: str, body: dict) -> dict:
    id_ = parse_uuid(id_)
    if not user.employee_id:
        raise _not_linked()
    b = parse(Obj({'status': Enum(('confirmed', 'changes_requested')), 'note': Str(trim=True, max=1000, nullish=True)}), body)
    n = execute("""UPDATE kye_response r SET status = %s, note = %s, responded_at = now() FROM kye_campaign c
                    WHERE r.campaign_id = %s AND r.employee_id = %s AND c.id = r.campaign_id AND c.status = 'open' AND c.company_id = %s""",
                [b['status'], b.get('note') or None, id_, user.employee_id, company_id_of(user)])
    if not n:
        raise NotFoundError('nothing to confirm for this campaign')
    return {'ok': True}


# ================================================================ sign-in pop-ups

def popups(user: AuthUser) -> dict:
    from hajiri.engage.audience import audience_sql, placement_of
    company_id = company_id_of(user)
    params: list = [company_id, user.id, user.id, user.id]
    cond = audience_sql('a', placement_of(user.employee_id), params, True)
    rs = rows(f"""
        SELECT a.id, a.title, a.body, a.requires_ack, a.created_at, coalesce(e.name_en, au.name) AS author_name
          FROM announcement a JOIN app_user au ON au.id = a.author_id LEFT JOIN employee e ON e.id = a.author_employee_id
         WHERE a.company_id = %s AND a.deleted_at IS NULL AND a.popup AND a.author_id <> %s
           AND (a.expires_on IS NULL OR a.expires_on >= (now() AT TIME ZONE 'Asia/Kathmandu')::date) AND (a.popup_until IS NULL OR a.popup_until >= (now() AT TIME ZONE 'Asia/Kathmandu')::date)
           AND NOT EXISTS (SELECT 1 FROM announcement_popup_seen s WHERE s.announcement_id = a.id AND s.user_id = %s)
           AND NOT EXISTS (SELECT 1 FROM announcement_ack k WHERE k.announcement_id = a.id AND k.user_id = %s)
           AND {cond}
         ORDER BY a.created_at DESC LIMIT 5""", params)
    return {'popups': [{'id': str(x['id']), 'title': x['title'], 'body': x['body'], 'requiresAck': x['requires_ack'],
                        'createdAt': x['created_at'], 'authorName': x['author_name']} for x in rs]}


def dismiss_popup(user: AuthUser, id_: str, body: dict | None = None) -> dict:
    from hajiri.engage.audience import audience_sql, placement_of
    id_ = parse_uuid(id_)
    params: list = [id_, company_id_of(user)]
    cond = audience_sql('a', placement_of(user.employee_id), params, True)
    a = one(f'SELECT a.requires_ack FROM announcement a WHERE a.id = %s AND a.company_id = %s AND a.deleted_at IS NULL AND a.popup AND {cond}',
            params)
    if not a:
        raise NotFoundError('notice not found')
    b = parse(Obj({'acknowledge': Bool(default=False)}), body or {})
    if a['requires_ack'] and not b['acknowledge']:
        raise BadRequestError('confirm that you have read this notice')
    with transaction.atomic():
        execute('INSERT INTO announcement_popup_seen (announcement_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', [id_, user.id])
        if a['requires_ack']:
            execute('INSERT INTO announcement_ack (announcement_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', [id_, user.id])
    return {'ok': True}


# ================================================================ registered devices

DEVICE_NOT_REGISTERED = 'Clock in from a registered device. Register this phone under My attendance and ask HR to approve it.'


def device_token_hash(token: str) -> str:
    return sha256_hex(f'device:{token}')


def assert_registered_device(company_id: str, employee_id: str, token: str | None) -> None:
    """
    When the company allows browser clock-in only from registered devices, the request must carry the token of a device
    HR approved for this employee. Raises a 403 with details {'code': 'device_not_registered'} otherwise.
    """
    if not relations_policy(str(company_id))['registeredDevicesOnly']:
        return
    if token and 16 <= len(token) <= 200:
        if rows("""UPDATE registered_device SET last_used_at = now() WHERE token_hash = %s AND employee_id = %s AND company_id = %s
                    AND status = 'approved' RETURNING id""", [device_token_hash(token), str(employee_id), str(company_id)]):
            return
    raise ForbiddenError(DEVICE_NOT_REGISTERED, {'code': 'device_not_registered'})


def my_devices(user: AuthUser) -> dict:
    if not user.employee_id:
        return {'devices': [], 'required': False}
    company_id = company_id_of(user)
    rs = rows("""SELECT id, label, status, requested_at, decided_at, last_used_at FROM registered_device
                  WHERE employee_id = %s AND company_id = %s AND status <> 'revoked' ORDER BY requested_at DESC""", [user.employee_id, company_id])
    return {'devices': [{**r, 'id': str(r['id'])} for r in rs], 'required': relations_policy(company_id)['registeredDevicesOnly']}


REGISTER_DEVICE = Obj({'token': Str(min=16, max=200, regex=r'^[A-Za-z0-9_-]+$'), 'label': _text(80)})


def register_device(user: AuthUser, body: dict, user_agent: str | None = None) -> dict:
    if not user.employee_id:
        raise _not_linked()
    company_id = company_id_of(user)
    b = parse(REGISTER_DEVICE, body)
    n = one("SELECT count(*)::int AS n FROM registered_device WHERE employee_id = %s AND status IN ('pending', 'approved')", [user.employee_id])
    if n['n'] >= 3:
        raise ConflictError('you already have three devices; remove one first')
    ua = (user_agent or '')[:200] or None
    with transaction.atomic():
        ins = one("""INSERT INTO registered_device (company_id, employee_id, user_id, token_hash, label, user_agent)
                     VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (token_hash) DO NOTHING RETURNING id""",
                  [company_id, user.employee_id, user.id, device_token_hash(b['token']), b['label'], ua])
        if not ins:
            raise ConflictError('this device is already registered')
        did = str(ins['id'])
        for uid in user_ids_with_role(company_id, 'hr'):
            if uid == user.id:
                continue
            notify(company_id=company_id, user_id=uid, template='relations.device.new', title='New clock-in device to approve',
                   body=f'{user.name} registered "{b["label"]}".', payload={'deviceId': did})
    return {'id': did, 'status': 'pending'}


def remove_my_device(user: AuthUser, id_: str) -> dict:
    id_ = parse_uuid(id_)
    n = execute("""UPDATE registered_device SET status = 'revoked', decided_at = now() WHERE id = %s AND employee_id = %s
                    AND status <> 'revoked'""", [id_, user.employee_id])
    if not n:
        raise NotFoundError('device not found')
    return {'ok': True}


DEVICE_STATUSES = ('pending', 'approved', 'rejected', 'revoked', 'all')


def list_devices(user: AuthUser, query: dict | None = None) -> dict:
    _require_hr(user)
    q = parse(Obj({'status': Enum(DEVICE_STATUSES, default='all')}), {k: v for k, v in (query or {}).items() if v not in (None, '')})
    rs = rows("""SELECT d.id, d.employee_id, e.name_en AS employee_name, e.emp_code, d.label, d.user_agent, d.status, d.requested_at,
                        d.decided_at, d.last_used_at
                   FROM registered_device d JOIN employee e ON e.id = d.employee_id WHERE d.company_id = %s AND (%s = 'all' OR d.status = %s)
                  ORDER BY (d.status = 'pending') DESC, d.requested_at DESC LIMIT 500""", [company_id_of(user), q['status'], q['status']])
    return {'devices': [{**r, 'id': str(r['id']), 'employee_id': str(r['employee_id'])} for r in rs]}


def decide_device(user: AuthUser, id_: str, action: str) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    action = parse(Enum(('approve', 'reject', 'revoke')), action)
    company_id = company_id_of(user)
    nxt = {'approve': 'approved', 'reject': 'rejected', 'revoke': 'revoked'}[action]
    if action == 'approve' and not has_role(user, 'admin'):
        if one('SELECT 1 AS x FROM registered_device WHERE id = %s AND employee_id = %s', [id_, user.employee_id]):
            raise ForbiddenError('someone else must approve your own clock-in device')
    from_ = ['approved', 'pending'] if action == 'revoke' else ['pending']
    with transaction.atomic():
        u = one("""UPDATE registered_device SET status = %s, decided_by = %s, decided_at = now()
                    WHERE id = %s AND company_id = %s AND status = ANY(%s::text[]) RETURNING employee_id, label""",
                [nxt, user.id, id_, company_id, from_])
        if not u:
            raise ConflictError('this device cannot be changed that way')
        audit(action, 'registered_device', actor_id=user.id, row_id=id_)
        eid = str(u['employee_id'])
        uid = user_id_for_employee(eid)
        if uid and action != 'revoke':
            notify(company_id=company_id, user_id=uid, employee_id=eid, template='relations.device.decided',
                   title='Device approved' if action == 'approve' else 'Device not approved',
                   body=f'"{u["label"]}" {"can now be used to clock in" if action == "approve" else "was not approved for clock-in"}.',
                   payload={'deviceId': id_})
    return {'ok': True}


# ================================================================ HR overview counts

def summary(user: AuthUser) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    g = one("""SELECT count(*)::int AS n FROM grievance WHERE company_id = %s AND status IN ('submitted', 'under_review', 'hearing_scheduled', 'appealed')
                AND user_id <> %s AND NOT (%s::uuid = ANY(excluded_user_ids))""", [company_id, user.id, user.id])['n']
    d = one("SELECT count(*)::int AS n FROM disciplinary_case WHERE company_id = %s AND status IN ('open', 'show_cause', 'responded')", [company_id])['n']
    dev = one("SELECT count(*)::int AS n FROM registered_device WHERE company_id = %s AND status = 'pending'", [company_id])['n']
    kye = one("SELECT count(*)::int AS n FROM kye_campaign WHERE company_id = %s AND status = 'open'", [company_id])['n']
    return {'openGrievances': g, 'openCases': d, 'pendingDevices': dev, 'openKye': kye, 'isHr': has_role(user, 'hr')}


# ================================================================ daily job

def _manager_user_of(employee_id: str, on_date: str) -> str | None:
    r = one("""SELECT u.id FROM employee_assignment a JOIN app_user u ON u.employee_id = a.manager_id AND u.status = 'active'
                WHERE a.employee_id = %s AND a.valid @> %s::date""", [employee_id, on_date])
    return str(r['id']) if r else None


def relations_daily(now: datetime | None = None) -> dict:
    """
    Once a day: end acting and deputation assignments that ran out, remind HR (and the manager) about milestones, flag
    disciplinary responses that are overdue, remind KYE stragglers three days before the due date, and close decided
    grievances whose appeal window has passed.
    """
    now = now or _now()
    today_ = npt_today(now).isoformat()
    out = {'assignmentsEnded': 0, 'milestoneReminders': 0, 'overdueResponses': 0, 'kyeReminders': 0, 'grievancesClosed': 0}

    with transaction.atomic():
        ended = rows("""UPDATE employee_temp_assignment SET status = 'ended', ended_at = now() WHERE status = 'active' AND end_date < %s::date
                         RETURNING id, company_id, employee_id, kind""", [today_])
        for t in ended:
            out['assignmentsEnded'] += 1
            eid = str(t['employee_id'])
            uid = user_id_for_employee(eid)
            if uid:
                notify(company_id=str(t['company_id']), user_id=uid, employee_id=eid, template='relations.assignment.ended',
                       title='Acting assignment ended' if t['kind'] == 'acting' else 'Deputation ended',
                       body='Your temporary assignment has ended; your regular role continues.', payload={'assignmentId': str(t['id'])})

    for c in rows('SELECT id FROM company'):
        cid = str(c['id'])
        lst = milestones(cid, today_, 30)
        hr = user_ids_with_role(cid, 'hr')
        for m in lst:
            # Service anniversaries on the day; contract ends and retirements 30 and 7 days ahead and on the day.
            days_left = (date.fromisoformat(m['date']) - date.fromisoformat(today_)).days
            key = None
            if m['kind'] == 'service' and days_left == 0:
                key = 'day'
            if m['kind'] in ('contract_end', 'retirement') and days_left in (30, 7, 0):
                key = f'd{days_left}'
            if m['kind'] == 'retirement' and days_left < 0:
                key = 'past'
            if not key:
                continue
            with transaction.atomic():
                if not _remind_once(cid, f"milestone.{m['kind']}", f"{m['employeeId']}:{m['date']}:{key}"):
                    continue
                if m['kind'] == 'service':
                    title = f"{m['name']}: {m['detail']}"
                    body = f"{m['name']} completes {m['detail']} today ({bs_label(m['date'])})."
                else:
                    title = f"{m['detail']}: {m['name']}"
                    body = (f"{m['name']} ({m['empCode']}): {m['detail'].lower()} on {bs_label(m['date'])}. "
                            'Plan the next step under Employee relations, Milestones.')
                targets = list(dict.fromkeys(hr))
                mgr = _manager_user_of(m['employeeId'], today_)
                if mgr and mgr not in targets:
                    targets.append(mgr)
                for uid in targets:
                    notify(company_id=cid, user_id=uid, template=f"relations.milestone.{m['kind']}", title=title, body=body,
                           payload={'employeeId': m['employeeId'], 'date': m['date']})
                out['milestoneReminders'] += 1

    with transaction.atomic():
        for k in rows("""UPDATE disciplinary_case SET overdue_notified = true WHERE status = 'show_cause' AND response_due < %s::date
                          AND NOT overdue_notified RETURNING id, company_id, number""", [today_]):
            for uid in user_ids_with_role(str(k['company_id']), 'hr'):
                notify(company_id=str(k['company_id']), user_id=uid, template='relations.disciplinary.overdue',
                       title=f"Case D-{k['number']}: no response yet", body='The response deadline has passed. You can now decide the case.',
                       payload={'caseId': str(k['id'])})
            out['overdueResponses'] += 1

    with transaction.atomic():
        for k in rows("""UPDATE kye_campaign SET reminded = true WHERE status = 'open' AND NOT reminded AND due_date - 3 <= %s::date
                          RETURNING id, company_id, title, due_date::text AS due_date""", [today_]):
            for p in rows("SELECT employee_id FROM kye_response WHERE campaign_id = %s AND status = 'pending'", [k['id']]):
                eid = str(p['employee_id'])
                uid = user_id_for_employee(eid)
                if uid:
                    notify(company_id=str(k['company_id']), user_id=uid, employee_id=eid, template='relations.kye.reminder',
                           title=f"Please review your profile: {k['title']}", body=f"Confirm your details by {bs_label(k['due_date'])}.",
                           payload={'campaignId': str(k['id'])})
                    out['kyeReminders'] += 1

    for g in rows("SELECT g.id, g.company_id, g.decided_at FROM grievance g WHERE g.status = 'decided' AND g.decided_at IS NOT NULL"):
        p = relations_policy(str(g['company_id']))
        if now - g['decided_at'] > timedelta(days=p['appealDays']):
            execute("UPDATE grievance SET status = 'closed', updated_at = now() WHERE id = %s AND status = 'decided'", [g['id']])
            out['grievancesClosed'] += 1
    return out
