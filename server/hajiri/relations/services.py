"""
Employee relations (port of apps/api/src/modules/relations/service.ts and routes.ts): grievances, disciplinary action,
milestones, acting and deputation, Know Your Employee, sign-in pop-ups and registered clock-in devices.

Grievances are heard by a committee that HR names; they are never routed to the line manager. Free text is encrypted,
and every look at a case by someone other than the person it is about is audited. Exclusions (conflicts of interest)
only grow (an administrator may lift one), and nobody can make themselves the only handler of a case.

One function per TS route. They take the signed-in AuthUser and a camelCase body shaped like the TS one, validate it
with the serializers in serializers.py, raise hajiri.core.errors with the TS messages and return dicts in the TS
response shape. Every query is scoped to the user's ACTIVE company (`company_id_of(user)`): a record of another
company is simply not found. People of a company (committee members, HR to notify, exclusions) are everyone who can
act there: home accounts, members of the group with access and group-role holders (hajiri.core.users).

Used by other slices:
- assert_registered_device(company_id, employee_id, token): punch calls it before a browser or app clock-in.
- relations_daily(now=None): the daily job 'relations' (jobs.py).
- relations_policy(company_id), completeness(company_id, employee_id=None), milestones(company_id, from_iso, days).
"""
from __future__ import annotations

import os
import threading
import time
import uuid as _uuid
from datetime import date, datetime, timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import (
    BooleanField, Count, Exists, ExpressionWrapper, F, Max, OuterRef, Q, Subquery,
)
from django.db.models.functions import Coalesce, Now

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import now as _now, npt_today
from hajiri.core.collation import collate_key
from hajiri.core.crypto import field_cipher, sha256_hex
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.core.settings_store import get_setting, put_setting
from hajiri.core.users import company_users as _company_people, user_ids_with_role
from hajiri.core.validation import choice_message, parse_uuid, plain, validate
from hajiri.db.models import (
    Announcement, AnnouncementAck, AnnouncementPopupSeen, Branch, Company, Designation, DisciplinaryCase,
    DisciplinaryStage, Employee, EmployeeAssignment, EmployeeEducation, EmployeeEmergencyContact, EmployeeExperience,
    EmployeeFamily, EmployeeTempAssignment, Grievance, GrievanceCommitteeMember, GrievanceFile, GrievanceNote,
    IssuedLetter, KyeCampaign, KyeResponse, OrgUnit, RegisteredDevice, RelationsReminderSent, StoredFile,
)
from hajiri.notifications.service import notify, user_id_for_employee

from .filters import AssignmentFilter, CaseFilter, DeviceFilter, GrievanceFilter, apply
from .serializers import (
    DEVICE_ACTIONS, KYE_SECTIONS, AppealDecisionIn, AppealIn, AssignmentEndIn, AssignmentIn, AssignmentsQueryIn,
    CampaignIn, CaseDecisionIn, CaseIn, CaseResponseIn, CasesQueryIn, CommitteeIn, ContractIn, DeviceIn,
    DevicesQueryIn, DismissIn, EmployeeQueryIn, GrievanceDecisionIn, GrievanceIn, GrievanceQueryIn, GrievanceUpdateIn,
    KyeResponseIn, LetterLinkIn, MilestonesQueryIn, NoteIn, PolicyIn, ReasonIn, ShowCauseIn, StagesIn,
)

DAY = timedelta(days=1)

GStatus = Grievance.Status
CStatus = DisciplinaryCase.Status
TStatus = EmployeeTempAssignment.Status
TKind = EmployeeTempAssignment.Kind
DStatus = RegisteredDevice.Status
KStatus = KyeCampaign.Status
RStatus = KyeResponse.Status
ACTIVE = Employee.Status.ACTIVE

# ---------------------------------------------------------------- policy


def relations_policy(company_id: str) -> dict:
    """The company's relations settings; a stored value that no longer validates falls back to the defaults."""
    s = PolicyIn(data=get_setting(company_id, 'relations.policy') or {})
    return dict(s.validated_data) if s.is_valid() else validate(PolicyIn, {})


# ---------------------------------------------------------------- helpers

def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def _q(query: dict | None) -> dict:
    """A query string as the TS route read it: empty values are absent."""
    return {k: v for k, v in (query or {}).items() if v not in (None, '')}


def _flag(q: Q) -> ExpressionWrapper:
    return ExpressionWrapper(q, output_field=BooleanField())


def is_hr(u: AuthUser) -> bool:
    return has_role(u, 'hr')


def _require_hr(u: AuthUser) -> None:
    # requireRole('hr') on the TS route.
    if not is_hr(u):
        raise ForbiddenError('requires role hr')


def today() -> str:
    return npt_today().isoformat()


_uuid_lock = threading.Lock()
_uuid_last = [0, 0]   # [unix ms, sequence within that ms]


def new_id() -> str:
    """A time-ordered UUIDv7 (RFC 9562), like PostgreSQL's uuidv7(). Encrypted columns bind the row id into their
    associated data, so the id is needed before the row is written. Ids made in the same millisecond keep their order
    (a 12-bit counter in rand_a), so notes saved in one transaction sort as written."""
    with _uuid_lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _uuid_last[0]:
            ms, seq = _uuid_last[0], _uuid_last[1] + 1
            if seq > 0xFFF:
                ms, seq = ms + 1, 0
        else:
            seq = 0
        _uuid_last[0], _uuid_last[1] = ms, seq
    rand_b = int.from_bytes(os.urandom(8)) & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | seq << 64 | 0b10 << 62 | rand_b
    return str(_uuid.UUID(int=value))


def next_number(model, company_id: str) -> int:
    """Next per-company case number. A no-key lock on the company row serialises two filings (it does not block
    inserts that reference the company), and the unique (company, number) index is the last word."""
    assert model in (Grievance, DisciplinaryCase)
    Company.objects.select_for_update(no_key=True).filter(pk=company_id).first()
    return (model.objects.for_company(company_id).aggregate(n=Max('number'))['n'] or 0) + 1


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
    """True the first time a reminder is recorded (race-safe: get_or_create re-reads when a parallel insert wins)."""
    _, created = RelationsReminderSent.objects.get_or_create(company_id=company_id, kind=kind, ref=ref)
    return created


def _iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def _row(r: dict, ids: tuple = (), dates: tuple = ()) -> dict:
    """A values() row in the shape the TS routes returned: uuids as strings, dates as 'YYYY-MM-DD'."""
    for k in ids:
        if k in r:
            r[k] = _s(r[k])
    for k in dates:
        if k in r:
            r[k] = _iso(r[k])
    return r


def _employee_in_company(company_id: str, employee_id: str) -> dict:
    r = Employee.objects.for_company(company_id).filter(pk=employee_id).values('id', 'name_en', 'emp_code', 'status').first()
    if not r:
        raise NotFoundError('employee not found')
    return r


def _not_linked() -> BadRequestError:
    return BadRequestError('Your account is not linked to an employee record.')


def add_years(d: date, years: int) -> date:
    """`d + make_interval(years => n)` as PostgreSQL computes it: 29 February becomes 28 February in other years."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


# ================================================================ policy routes

def get_policy(user: AuthUser) -> dict:
    _require_hr(user)
    return {'policy': relations_policy(company_id_of(user))}


def save_policy(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    p = validate(PolicyIn, body)
    p['serviceYears'] = sorted(set(p['serviceYears']))
    with transaction.atomic():
        put_setting(company_id, 'relations.policy', p)
        audit('update', 'company_setting', actor_id=user.id, row_id=None, after={'key': 'relations.policy', **p})
    return {'policy': p}


# ================================================================ grievances

GRIEVANCE_CATEGORIES = tuple(Grievance.Category.values)
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
OPEN_GRIEVANCE = (GStatus.SUBMITTED, GStatus.UNDER_REVIEW, GStatus.HEARING_SCHEDULED)
DONE_GRIEVANCE = (GStatus.DECIDED, GStatus.CLOSED, GStatus.WITHDRAWN)
_GRIEVANCE_IDS = ('id', 'company_id', 'employee_id', 'user_id', 'decided_by', 'appeal_decided_by')


def committee_members(company_id: str):
    return GrievanceCommitteeMember.objects.for_company(company_id)


def is_committee_member(company_id: str, user_id: str) -> bool:
    return committee_members(company_id).filter(user_id=user_id).exists()


def _grievance_row(r: dict) -> dict:
    _row(r, _GRIEVANCE_IDS)
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
    """HR and the committee (people who can act in the company), minus anyone excluded from the case and the
    complainant."""
    hr = user_ids_with_role(company_id, 'hr')
    active = {str(p['id']) for p in _company_people(company_id)}
    committee_ids = [i for i in (str(u) for u in committee_members(company_id).values_list('user_id', flat=True)) if i in active]
    excluded = {str(x) for x in excluded_user_ids}
    return [i for i in dict.fromkeys([*hr, *committee_ids]) if i != user_id and i not in excluded]


def committee(user: AuthUser) -> dict:
    company_id = company_id_of(user)
    member = is_committee_member(company_id, user.id)
    if not is_hr(user) and not member:
        return {'members': [], 'isMember': False}
    members = (committee_members(company_id).order_by('user__name')
               .values('user_id', 'added_at', name=F('user__name'), email=F('user__email')))
    return {'members': [{**m, 'user_id': str(m['user_id'])} for m in members], 'isMember': member}


def set_committee(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    ids = list(dict.fromkeys(plain(validate(CommitteeIn, body))['userIds']))
    people = {str(p['id']) for p in _company_people(company_id)}
    if any(i not in people for i in ids):
        raise BadRequestError('every member must be an active user in your company')
    with transaction.atomic():
        committee_members(company_id).exclude(user_id__in=ids).delete()
        GrievanceCommitteeMember.objects.bulk_create(
            [GrievanceCommitteeMember(company_id=company_id, user_id=i, added_by_id=user.id) for i in ids], ignore_conflicts=True)
        audit('update', 'grievance_committee_member', actor_id=user.id, after={'userIds': ids})
    return {'ok': True, 'members': len(ids)}


def company_users(user: AuthUser) -> list[dict]:
    """Active people who can act in the company (auth.users() in the React pages, for the committee and exclusions)."""
    people = [{'id': str(p['id']), 'name': p['name'], 'email': p['email']} for p in _company_people(company_id_of(user))]
    people.sort(key=lambda p: (collate_key(p['name']), collate_key(p['email'])))
    return people


def file_grievance(user: AuthUser, body: dict) -> dict:
    from hajiri.files.store import attach_file
    company_id = company_id_of(user)
    if not user.employee_id:
        raise _not_linked()
    b = plain(validate(GrievanceIn, body))
    with transaction.atomic():
        id_ = new_id()
        number = next_number(Grievance, company_id)
        Grievance.objects.create(
            id=id_, company_id=company_id, number=number, employee_id=user.employee_id, user_id=user.id, category=b['category'],
            subject_enc=enc_text(b['subject'], grievance_aad(company_id, id_, 'subject')),
            description_enc=enc_text(b['description'], grievance_aad(company_id, id_, 'description')),
            concerns_enc=enc_text(b['concerns'], grievance_aad(company_id, id_, 'concerns')) if b.get('concerns') else None,
            excluded_user_ids=b['excludeUserIds'])
        for file_id in b['fileIds']:
            attach_file(company_id=company_id, file_id=file_id, user_id=user.id, purpose='grievance_attachment',
                        attached_to=f'grievance:{id_}')
            GrievanceFile.objects.create(grievance_id=id_, file_id=file_id)
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


_GRIEVANCE_FIELDS = tuple(f.attname if f.attname in ('company_id', 'employee_id', 'user_id') else f.name
                          for f in Grievance._meta.concrete_fields)
_LIST_HIDDEN = ('description_enc', 'concerns_enc', 'findings_enc', 'appeal_reason_enc', 'appeal_note_enc')


def grievances(company_id: str, fields: tuple = _GRIEVANCE_FIELDS):
    """The company's grievances with the complainant's name and code next to each row (dicts)."""
    return Grievance.objects.for_company(company_id).values(
        *fields, employee_name=F('employee__name_en'), emp_code=F('employee__emp_code'))


def load_grievance(user: AuthUser, id_: str, lock: bool = False) -> tuple[dict, str]:
    qs = grievances(company_id_of(user)).filter(pk=id_)
    if lock:
        qs = qs.select_for_update(of=('self',))
    r = qs.first()
    g = _grievance_row(r) if r else None
    role = grievance_role(user, g) if g else None
    if not g or not role:
        raise NotFoundError('grievance not found')
    return g, role


def list_grievances(user: AuthUser, query: dict | None = None) -> dict:
    company_id = company_id_of(user)
    q = validate(GrievanceQueryIn, _q(query))
    if q['scope'] == 'mine':
        rs = grievances(company_id).filter(user_id=user.id).order_by('-created_at')
        return {'grievances': [grievance_view(_grievance_row(g), 'complainant') for g in rs]}
    member = is_committee_member(company_id, user.id)
    if not is_hr(user) and not member:
        raise ForbiddenError('only HR and the grievance committee handle grievances')
    listed = tuple(f for f in _GRIEVANCE_FIELDS if f not in _LIST_HIDDEN)
    rs = grievances(company_id, listed).exclude(user_id=user.id).exclude(excluded_user_ids__contains=[user.id])
    rs = apply(GrievanceFilter, {'status': q.get('status')}, rs).order_by(_flag(Q(status__in=DONE_GRIEVANCE)), '-created_at')[:500]
    role = 'hr' if is_hr(user) else 'committee'
    # Lists show the subject only; the full text opens (and is audited) per case.
    out = []
    for g in rs:
        g = _grievance_row(g)
        for col in _LIST_HIDDEN:
            g[col] = None
        out.append(grievance_view(g, role))
    return {'grievances': out}


def grievance_detail(user: AuthUser, id_: str) -> dict:
    g, role = load_grievance(user, parse_uuid(id_))
    notes = GrievanceNote.objects.filter(grievance_id=g['id'])
    if role == 'complainant':
        notes = notes.filter(shared=True)
    notes = notes.order_by('created_at', 'id').values('id', 'author_id', 'shared', 'body_enc', 'created_at',
                                                       author_name=F('author__name'))
    files = (StoredFile.objects.for_company(g['company_id'])
             .filter(grievance_files__grievance_id=g['id'], deleted_at__isnull=True).values('id', 'filename', 'mime', 'bytes'))
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
        'canAppeal': role == 'complainant' and g['status'] == GStatus.DECIDED and not g['appealed_at'] and bool(appeal_until)
        and appeal_until > _now(),
        'appealUntil': appeal_until,
    }


def grievance_file(user: AuthUser, id_: str, file_id: str) -> dict:
    """The stored file (store.read_file shape) if the user can see the case."""
    from hajiri.files.store import read_file
    g, role = load_grievance(user, parse_uuid(id_))
    file_id = parse_uuid(file_id)
    if not GrievanceFile.objects.filter(grievance_id=g['id'], file_id=file_id).exists():
        raise NotFoundError('file not found')
    if role != 'complainant':
        audit('view_file', 'grievance', actor_id=user.id, row_id=g['id'], after={'fileId': file_id})
    return read_file(g['company_id'], file_id)


def _touch(g: dict, **fields) -> None:
    Grievance.objects.filter(pk=g['id']).update(updated_at=Now(), **fields)


def add_note(user: AuthUser, id_: str, body: dict) -> dict:
    id_ = parse_uuid(id_)
    b = validate(NoteIn, body)
    with transaction.atomic():
        g, role = load_grievance(user, id_, lock=True)
        if g['status'] in (GStatus.CLOSED, GStatus.WITHDRAWN):
            raise ConflictError(f"this grievance is {g['status']}")
        shared = True if role == 'complainant' else b['shared']
        note_id = new_id()
        GrievanceNote.objects.create(id=note_id, grievance_id=g['id'], author_id=user.id, shared=shared,
                                     body_enc=enc_text(b['body'], grievance_note_aad(g['id'], note_id)))
        _touch(g)
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


def update_grievance(user: AuthUser, id_: str, body: dict) -> dict:
    """Committee or HR: acknowledge, schedule a hearing, or exclude someone with a conflict of interest (HR only)."""
    id_ = parse_uuid(id_)
    b = plain(validate(GrievanceUpdateIn, body))
    excluding = 'excludeUserIds' in b
    exclude = b.get('excludeUserIds')
    with transaction.atomic():
        g, role = load_grievance(user, id_, lock=True)
        if role == 'complainant':
            raise ForbiddenError('the committee handles this grievance')
        if g['status'] not in OPEN_GRIEVANCE:
            raise ConflictError(f"this grievance is {g['status']}")
        if excluding and role != 'hr':
            raise ForbiddenError('only HR can exclude people from a case')
        if excluding and user.id in exclude:
            raise BadRequestError('you cannot exclude yourself; ask another HR person')
        if excluding:
            # Exclusions only grow (an admin may lift one), and nobody can make themselves the sole handler of a case.
            removed = [x for x in g['excluded_user_ids'] if x not in exclude]
            if removed and not has_role(user, 'admin'):
                raise ForbiddenError('only an administrator can lift an exclusion')
            others = [x for x in grievance_handlers(g['company_id'], g['user_id'], exclude) if x != user.id]
            if not others:
                raise BadRequestError('at least one other HR or committee member must be able to handle this case')
        status = b.get('status') or g['status']
        hearing_at = b.get('hearingAt')
        if hearing_at:
            status = GStatus.HEARING_SCHEDULED
        if status == GStatus.HEARING_SCHEDULED and not (hearing_at or g['hearing_at']):
            raise BadRequestError('set the hearing date and time')
        place = b.get('hearingPlace')
        fields: dict = {'status': status}
        if 'hearingAt' in b:
            try:
                fields['hearing_at'] = datetime.fromisoformat(hearing_at) if hearing_at else None
            except ValueError:
                # The shape is checked by the serializer; an impossible date or time (31 February, 25:00) was refused
                # by PostgreSQL in the port, with the database error's message.
                raise BadRequestError('invalid value format') from None
        if 'hearingPlace' in b:
            fields['hearing_place'] = place
        if excluding:
            fields['excluded_user_ids'] = exclude
        _touch(g, **fields)
        after = {'status': status}
        if 'hearingAt' in b:
            after['hearingAt'] = hearing_at
        if excluding:
            after['excluded'] = exclude
        audit('update', 'grievance', actor_id=user.id, row_id=g['id'], before={'status': g['status']}, after=after)
        if hearing_at:
            notify(company_id=g['company_id'], user_id=g['user_id'], employee_id=g['employee_id'], template='relations.grievance.hearing',
                   title=f"Hearing set for your grievance G-{g['number']}",
                   body=f"The committee will hear your grievance on {bs_label(hearing_at)}{f' at {place}' if place else ''}.",
                   payload={'grievanceId': g['id']}, channels=('push',))
        elif status == GStatus.UNDER_REVIEW and g['status'] == GStatus.SUBMITTED:
            notify(company_id=g['company_id'], user_id=g['user_id'], employee_id=g['employee_id'], template='relations.grievance.review',
                   title=f"Your grievance G-{g['number']} is under review", body='The grievance committee has started reviewing it.',
                   payload={'grievanceId': g['id']})
    return {'ok': True}


def decide_grievance(user: AuthUser, id_: str, body: dict) -> dict:
    id_ = parse_uuid(id_)
    b = validate(GrievanceDecisionIn, body)
    with transaction.atomic():
        g, role = load_grievance(user, id_, lock=True)
        if role == 'complainant':
            raise ForbiddenError('the committee decides this grievance')
        if g['status'] not in OPEN_GRIEVANCE:
            raise ConflictError(f"this grievance is {g['status']}")
        _touch(g, status=GStatus.DECIDED, outcome=b['outcome'], decided_by_id=user.id, decided_at=Now(),
               findings_enc=enc_text(b['findings'], grievance_aad(g['company_id'], g['id'], 'findings')))
        audit('decide', 'grievance', actor_id=user.id, row_id=g['id'], after={'outcome': b['outcome']})
        p = relations_policy(g['company_id'])
        notify(company_id=g['company_id'], user_id=g['user_id'], employee_id=g['employee_id'], template='relations.grievance.decided',
               title=f"Decision on your grievance G-{g['number']}",
               body=f"The committee has decided. Read the findings; you can appeal within {p['appealDays']} days.",
               payload={'grievanceId': g['id']}, channels=('push',))
    return {'ok': True}


def appeal_grievance(user: AuthUser, id_: str, body: dict) -> dict:
    id_ = parse_uuid(id_)
    b = validate(AppealIn, body)
    with transaction.atomic():
        g, role = load_grievance(user, id_, lock=True)
        if role != 'complainant':
            raise ForbiddenError('only the person who filed the grievance can appeal')
        if g['status'] != GStatus.DECIDED:
            raise ConflictError('you can appeal once a decision has been made')
        if g['appealed_at']:
            raise ConflictError('this grievance has already been appealed')
        p = relations_policy(g['company_id'])
        if _now() - g['decided_at'] > timedelta(days=p['appealDays']):
            raise ConflictError(f"the {p['appealDays']}-day appeal window has passed")
        _touch(g, status=GStatus.APPEALED, appealed_at=Now(),
               appeal_reason_enc=enc_text(b['reason'], grievance_aad(g['company_id'], g['id'], 'appeal_reason')))
        audit('appeal', 'grievance', actor_id=user.id, row_id=g['id'])
        # Appeals are heard by HR, not by the committee that decided.
        for uid in user_ids_with_role(g['company_id'], 'hr'):
            if uid == g['user_id'] or uid in g['excluded_user_ids']:
                continue
            notify(company_id=g['company_id'], user_id=uid, template='relations.grievance.appeal', title=f"Grievance G-{g['number']} appealed",
                   body='The employee appealed the decision. HR hears appeals.', payload={'grievanceId': g['id']})
    return {'ok': True}


def decide_appeal(user: AuthUser, id_: str, body: dict) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    b = validate(AppealDecisionIn, body)
    with transaction.atomic():
        g, role = load_grievance(user, id_, lock=True)
        if role != 'hr':
            raise ForbiddenError('HR hears appeals')
        if g['status'] != GStatus.APPEALED:
            raise ConflictError('this grievance has no open appeal')
        if g['decided_by'] == user.id:
            raise ForbiddenError('someone other than the person who decided must hear the appeal')
        changed = b['outcome'] == Grievance.AppealOutcome.DECISION_CHANGED
        if changed and not b.get('newOutcome'):
            raise BadRequestError('choose the new outcome')
        fields = {'outcome': b['newOutcome']} if changed else {}
        _touch(g, status=GStatus.CLOSED, appeal_outcome=b['outcome'], appeal_decided_by_id=user.id, appeal_decided_at=Now(),
               appeal_note_enc=enc_text(b['note'], grievance_aad(g['company_id'], g['id'], 'appeal_note')), **fields)
        after = {'outcome': b['outcome']}
        if 'newOutcome' in b:
            after['newOutcome'] = b['newOutcome']
        audit('appeal_decide', 'grievance', actor_id=user.id, row_id=g['id'], after=after)
        notify(company_id=g['company_id'], user_id=g['user_id'], employee_id=g['employee_id'], template='relations.grievance.appeal_decided',
               title=f"Appeal decided: G-{g['number']}",
               body=('The original decision stands. Read the note for the reasons.' if not changed
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
        _touch(g, status=GStatus.WITHDRAWN)
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
        if g['status'] != GStatus.DECIDED:
            raise ConflictError('only a decided grievance can be closed')
        _touch(g, status=GStatus.CLOSED)
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
CASE_CATEGORIES = tuple(DisciplinaryCase.Category.values)
CASE_CATEGORY_LABEL = {'attendance': 'Attendance', 'conduct': 'Conduct', 'performance': 'Performance', 'policy': 'Policy breach',
                       'safety': 'Safety', 'misconduct': 'Misconduct', 'other': 'Other'}
CASE_STATUS_LABEL = {'open': 'Under investigation', 'show_cause': 'Waiting for response', 'responded': 'Response received',
                     'decided': 'Decided', 'withdrawn': 'Withdrawn'}
OPEN_CASE = (CStatus.OPEN, CStatus.SHOW_CAUSE, CStatus.RESPONDED)
VISIBLE_TO_EMPLOYEE = (CStatus.SHOW_CAUSE, CStatus.RESPONDED, CStatus.DECIDED)
Effect = DisciplinaryStage.Effect


def stages_of(company_id: str):
    return DisciplinaryStage.objects.for_company(company_id)


def ensure_stages(company_id: str) -> None:
    if stages_of(company_id).exists():
        return
    DisciplinaryStage.objects.bulk_create(
        [DisciplinaryStage(company_id=company_id, code=s['code'], name=s['name'], severity=s['severity'], effect=s['effect'],
                           letter_template_code=s['letter'], sort=i) for i, s in enumerate(DEFAULT_STAGES)],
        ignore_conflicts=True)


def list_stages(user: AuthUser) -> dict:
    company_id = company_id_of(user)
    ensure_stages(company_id)
    rs = stages_of(company_id).order_by('sort', 'severity').values('id', 'code', 'name', 'severity', 'effect',
                                                                    'letter_template_code', 'sort', 'active')
    return {'stages': [{**r, 'id': str(r['id'])} for r in rs]}


def save_stages(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = validate(StagesIn, body)
    ensure_stages(company_id)
    with transaction.atomic():
        for i, s in enumerate(b['stages']):
            fields = {'code': s['code'], 'name': s['name'], 'severity': s['severity'], 'effect': s['effect'],
                      'letter_template_code': s.get('letterTemplateCode') or None, 'sort': i, 'active': s['active']}
            if s.get('id'):
                if not stages_of(company_id).filter(pk=s['id']).update(**fields):
                    raise NotFoundError('stage not found')
            else:
                DisciplinaryStage.objects.create(company_id=company_id, **fields)
        audit('update', 'disciplinary_stage', actor_id=user.id, after={'count': len(b['stages'])})
    return {'ok': True}


_CASE_FIELDS = tuple(f.attname for f in DisciplinaryCase._meta.concrete_fields)
_CASE_IDS = ('id', 'company_id', 'employee_id', 'stage_id', 'exit_id', 'letter_id', 'opened_by', 'decided_by')
_CASE_DATES = ('incident_date', 'response_due', 'suspension_from', 'suspension_to')


def cases(company_id: str):
    """The company's disciplinary cases with the employee, stage and linked letter next to each row (dicts)."""
    return DisciplinaryCase.objects.for_company(company_id).values(
        *_CASE_FIELDS, employee_name=F('employee__name_en'), emp_code=F('employee__emp_code'), stage_name=F('stage__name'),
        stage_effect=F('stage__effect'), stage_letter=F('stage__letter_template_code'), letter_ref=F('letter__ref_no'))


def _case_row(k: dict) -> dict:
    k['opened_by'], k['decided_by'] = k.pop('opened_by_id', None), k.pop('decided_by_id', None)
    return _row(k, _CASE_IDS, _CASE_DATES)


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
    qs = cases(company_id_of(user)).filter(pk=id_)
    if lock:
        qs = qs.select_for_update(of=('self',))
    r = qs.first()
    if not r:
        raise NotFoundError('case not found')
    return _case_row(r)


def _case_order(qs):
    return qs.order_by(_flag(Q(status__in=(CStatus.DECIDED, CStatus.WITHDRAWN))), '-created_at')


def list_cases(user: AuthUser, query: dict | None = None) -> dict:
    _require_hr(user)
    q = plain(validate(CasesQueryIn, _q(query)))
    rs = apply(CaseFilter, q, cases(company_id_of(user)))
    return {'cases': [case_view(_case_row(k), False) for k in _case_order(rs)[:500]]}


def my_cases(user: AuthUser) -> dict:
    """The employee's own cases, once HR has asked them to explain or has decided; investigations in progress stay with HR."""
    if not user.employee_id:
        return {'cases': []}
    rs = (cases(company_id_of(user)).filter(employee_id=user.employee_id, status__in=VISIBLE_TO_EMPLOYEE)
          .order_by('-created_at'))
    return {'cases': [case_view(_case_row(k), True) for k in rs]}


def case_detail(user: AuthUser, id_: str) -> dict:
    k = load_case(user, parse_uuid(id_))
    own = user.employee_id == k['employee_id']
    if not is_hr(user) and not (own and k['status'] in VISIBLE_TO_EMPLOYEE):
        raise NotFoundError('case not found')
    if not own:
        audit('view', 'disciplinary_case', actor_id=user.id, row_id=k['id'])
    return {'case': case_view(k, own and not is_hr(user))}


def open_case(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = plain(validate(CaseIn, body))
    emp = _employee_in_company(company_id, b['employeeId'])
    if emp['status'] != ACTIVE:
        raise ConflictError('this employee has left')
    if b['incidentDate'] > today():
        raise BadRequestError('the incident date is in the future')
    if user.employee_id == b['employeeId']:
        raise ForbiddenError('you cannot open a case about yourself')
    with transaction.atomic():
        id_ = new_id()
        number = next_number(DisciplinaryCase, company_id)
        DisciplinaryCase.objects.create(id=id_, company_id=company_id, number=number, employee_id=b['employeeId'],
                                        category=b['category'], incident_date=b['incidentDate'], opened_by_id=user.id,
                                        incident_enc=enc_text(b['incident'], case_aad(company_id, id_, 'incident')))
        audit('create', 'disciplinary_case', actor_id=user.id, row_id=id_,
              after={'employeeId': b['employeeId'], 'category': b['category'], 'number': number})
    return {'id': id_, 'number': number}


def _update_case(k: dict, **fields) -> None:
    DisciplinaryCase.objects.filter(pk=k['id']).update(updated_at=Now(), **fields)


def show_cause(user: AuthUser, id_: str, body: dict) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    b = validate(ShowCauseIn, body)
    if b['responseDue'] < today():
        raise BadRequestError('the response deadline has already passed')
    with transaction.atomic():
        k = load_case(user, id_, lock=True)
        if k['status'] != CStatus.OPEN:
            raise ConflictError('a show-cause notice was already sent' if k['status'] == CStatus.SHOW_CAUSE else f"this case is {k['status']}")
        _update_case(k, status=CStatus.SHOW_CAUSE, show_cause_at=Now(), response_due=b['responseDue'],
                     show_cause_enc=enc_text(b['text'], case_aad(k['company_id'], k['id'], 'show_cause')))
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
    b = validate(CaseResponseIn, body)
    with transaction.atomic():
        k = load_case(user, id_, lock=True)
        if user.employee_id != k['employee_id']:
            raise NotFoundError('case not found')
        if k['status'] != CStatus.SHOW_CAUSE:
            raise ConflictError('you have already responded' if k['status'] == CStatus.RESPONDED else 'this case is not waiting for your response')
        _update_case(k, status=CStatus.RESPONDED, responded_at=Now(),
                     response_enc=enc_text(b['response'], case_aad(k['company_id'], k['id'], 'response')))
        audit('respond', 'disciplinary_case', actor_id=user.id, row_id=k['id'],
              after={'late': bool(k['response_due']) and k['response_due'] < today()})
        for uid in user_ids_with_role(k['company_id'], 'hr'):
            if uid == user.id:
                continue
            notify(company_id=k['company_id'], user_id=uid, template='relations.disciplinary.responded',
                   title=f"Case D-{k['number']}: response received", body=f"{k['employee_name']} responded to the show-cause notice.",
                   payload={'caseId': k['id']})
    return {'ok': True}


def log_suspension(*, company_id: str, employee_id: str, from_: str, to: str, stage_name: str, actor_id: str) -> None:
    """A suspension decided in a disciplinary case goes on the career timeline (it changes nothing permanent)."""
    from hajiri.people.career import log_career_event
    log_career_event(company_id=company_id, employee_id=employee_id, kind='suspension', effective_date=from_,
                     details={'endDate': to, 'reason': stage_name}, note='Recorded from a disciplinary case', actor_id=actor_id)


def decide_case(user: AuthUser, id_: str, body: dict) -> dict:
    from hajiri.lifecycle.service import record_exit
    _require_hr(user)
    id_ = parse_uuid(id_)
    b = plain(validate(CaseDecisionIn, body))
    with transaction.atomic():
        k = load_case(user, id_, lock=True)
        if k['status'] not in OPEN_CASE:
            raise ConflictError(f"this case is {k['status']}")
        if user.employee_id == k['employee_id']:
            raise ForbiddenError('you cannot decide a case about yourself')
        if k['status'] == CStatus.SHOW_CAUSE and k['response_due'] and k['response_due'] >= today() and not b['force']:
            raise ConflictError(f"the employee has until {bs_label(k['response_due'])} to respond; decide after that, or confirm you want to decide now")
        stage = None
        if b['stageId']:
            stage = stages_of(k['company_id']).filter(pk=b['stageId'], active=True).values('id', 'name', 'effect').first()
            if not stage:
                raise BadRequestError('choose an active stage')
            stage['id'] = str(stage['id'])
        exit_id = None
        effect = stage['effect'] if stage else None
        if effect == Effect.SUSPENSION:
            if not b.get('suspensionFrom') or not b.get('suspensionTo'):
                raise BadRequestError('enter the suspension dates')
            if b['suspensionTo'] < b['suspensionFrom']:
                raise BadRequestError('the suspension ends before it starts')
            log_suspension(company_id=k['company_id'], employee_id=k['employee_id'], from_=b['suspensionFrom'], to=b['suspensionTo'],
                           stage_name=stage['name'], actor_id=user.id)
        if effect == Effect.TERMINATION:
            if not b.get('lastWorkingDay'):
                raise BadRequestError('enter the last working day')
            exit_id = record_exit(company_id=k['company_id'], employee_id=k['employee_id'], kind='termination', voluntary=False,
                                  reason_category='disciplinary', reason=f"Disciplinary case D-{k['number']}",
                                  last_working_day=b['lastWorkingDay'], actor_id=user.id)
        suspended = effect == Effect.SUSPENSION
        _update_case(k, status=CStatus.DECIDED, stage_id=stage['id'] if stage else None,
                     decision_note_enc=enc_text(b['note'], case_aad(k['company_id'], k['id'], 'decision')),
                     suspension_from=b.get('suspensionFrom') if suspended else None,
                     suspension_to=b.get('suspensionTo') if suspended else None,
                     exit_id=exit_id, decided_by_id=user.id, decided_at=Now())
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
    b = plain(validate(LetterLinkIn, body))
    with transaction.atomic():
        k = load_case(user, id_, lock=True)
        letter_employee = (IssuedLetter.objects.for_company(k['company_id']).filter(pk=b['letterId'])
                           .values_list('employee_id', flat=True).first())
        if not letter_employee or str(letter_employee) != k['employee_id']:
            raise BadRequestError('that letter was not issued to this employee')
        _update_case(k, letter_id=b['letterId'])
        audit('link_letter', 'disciplinary_case', actor_id=user.id, row_id=k['id'], after={'letterId': b['letterId']})
    return {'ok': True}


def withdraw_case(user: AuthUser, id_: str, body: dict) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    b = validate(ReasonIn, body)
    with transaction.atomic():
        k = load_case(user, id_, lock=True)
        if k['status'] not in OPEN_CASE:
            raise ConflictError(f"this case is {k['status']}")
        _update_case(k, status=CStatus.WITHDRAWN, decided_by_id=user.id, decided_at=Now(),
                     decision_note_enc=enc_text(b['reason'], case_aad(k['company_id'], k['id'], 'decision')))
        audit('withdraw', 'disciplinary_case', actor_id=user.id, row_id=k['id'])
        if k['status'] != CStatus.OPEN:
            uid = user_id_for_employee(k['employee_id'])
            if uid:
                notify(company_id=k['company_id'], user_id=uid, employee_id=k['employee_id'], template='relations.disciplinary.withdrawn',
                       title='A disciplinary case was withdrawn', body='HR withdrew the case. No action will be taken.',
                       payload={'caseId': k['id']})
    return {'ok': True}


# ================================================================ completeness (KYE)

SECTION_LABEL = {
    'personal': 'Personal details', 'contact': 'Contact and address', 'emergency': 'Emergency contacts',
    'family': 'Family and nominees', 'education': 'Education', 'experience': 'Previous employment', 'ids': 'ID documents',
    'bank': 'Bank account', 'photo': 'Profile photo',
}
# Sections whose absence counts against the record; previous employment is optional (first jobs have none).
REQUIRED_SECTIONS = ('personal', 'contact', 'emergency', 'family', 'education', 'ids', 'bank', 'photo')


def _has(model) -> Exists:
    return Exists(model.objects.filter(employee=OuterRef('pk')))


def completeness(company_id: str, employee_id: str | None = None) -> list[dict]:
    qs = Employee.objects.for_company(company_id).filter(status=ACTIVE)
    if employee_id:
        qs = qs.filter(pk=employee_id)
    rs = qs.annotate(
        personal=_flag(Q(dob__isnull=False, gender__isnull=False, marital_status__isnull=False, blood_group__isnull=False)),
        contact=_flag((Q(personal_phone__isnull=False) | Q(phone__isnull=False)) & Q(permanent_address__isnull=False)),
        emergency=_has(EmployeeEmergencyContact), family=_has(EmployeeFamily), education=_has(EmployeeEducation),
        experience=_has(EmployeeExperience),
        ids=_flag(Q(citizenship_no_enc__isnull=False, pan_enc__isnull=False)),
        bank=_flag(Q(bank_account_enc__isnull=False)), photo=_flag(Q(avatar_file__isnull=False)),
    ).order_by('emp_code').values('id', 'emp_code', 'name_en', *KYE_SECTIONS)
    out = []
    for x in rs:
        missing = [s for s in REQUIRED_SECTIONS if not x[s]]
        pct = (len(REQUIRED_SECTIONS) - len(missing)) / len(REQUIRED_SECTIONS) * 100
        out.append({'employeeId': str(x['id']), 'empCode': x['emp_code'], 'name': x['name_en'], 'missing': missing,
                    'percent': int(pct + 0.5)})  # Math.round
    return out


def completeness_report(user: AuthUser, query: dict | None = None) -> dict:
    _require_hr(user)
    q = plain(validate(EmployeeQueryIn, _q(query)))
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
    start = date.fromisoformat(from_)
    end = start + timedelta(days=days)
    to = end.isoformat()
    active = Employee.objects.for_company(company_id).filter(status=ACTIVE)

    # Service anniversaries: join date + n years inside the window. The query keeps join dates that can land there
    # (a day of slack for 29 February); the exact date is computed below as PostgreSQL adds years.
    years = p['serviceYears']
    near = Q()
    for y in years:
        near |= Q(join_date__range=(add_years(start, -y) - DAY, add_years(end, -y) + DAY))
    if years:
        for s in active.filter(near).values('id', 'emp_code', 'name_en', 'join_date'):
            for y in years:
                d = add_years(s['join_date'], y)
                if start <= d <= end:
                    out.append({'kind': 'service', 'employeeId': str(s['id']), 'empCode': s['emp_code'], 'name': s['name_en'],
                                'date': d.isoformat(), 'detail': f"{y} {'year' if y == 1 else 'years'} of service"})

    age = p['retirementAge']
    probation_now = Subquery(EmployeeAssignment.objects.filter(employee=OuterRef('pk'), valid__contains=start)
                             .values('probation_end')[:1])
    rs = active.annotate(probation=Coalesce(probation_now, F('probation_end'))).filter(
        Q(contract_end__range=(start, end)) | Q(dob__lte=add_years(end, -age) + DAY)
        | Q(confirmed_on__isnull=True, probation__range=(start, end)) | Q(exit_date__range=(start, end)),
    ).values('id', 'emp_code', 'name_en', 'contract_end', 'dob', 'confirmed_on', 'probation', 'exit_date')
    for e in rs:
        base = {'employeeId': str(e['id']), 'empCode': e['emp_code'], 'name': e['name_en']}
        contract_end, exit_date = _iso(e['contract_end']), _iso(e['exit_date'])
        retire = _iso(add_years(e['dob'], age)) if e['dob'] else None
        probation = _iso(e['probation']) if e['confirmed_on'] is None else None
        if contract_end and from_ <= contract_end <= to:
            out.append({**base, 'kind': 'contract_end', 'date': contract_end, 'detail': 'Contract ends'})
        if retire and retire <= to and not exit_date:
            out.append({**base, 'kind': 'retirement', 'date': retire,
                        'detail': f'Past retirement age ({age})' if retire < from_ else f'Reaches retirement age ({age})'})
        if probation and from_ <= probation <= to:
            out.append({**base, 'kind': 'probation_end', 'date': probation, 'detail': 'Probation ends'})
        if exit_date and from_ <= exit_date <= to:
            out.append({**base, 'kind': 'exit', 'date': exit_date, 'detail': 'Last working day'})

    for t in (EmployeeTempAssignment.objects.for_company(company_id).filter(status=TStatus.ACTIVE, end_date__range=(start, end))
              .values('employee_id', 'end_date', 'kind', emp_code=F('employee__emp_code'), name_en=F('employee__name_en'))):
        out.append({'kind': 'assignment_end', 'employeeId': str(t['employee_id']), 'empCode': t['emp_code'], 'name': t['name_en'],
                    'date': _iso(t['end_date']),
                    'detail': 'Acting assignment ends' if t['kind'] == TKind.ACTING else 'Deputation ends'})
    out.sort(key=lambda m: (m['date'], collate_key(m['name'])))
    return out


def list_milestones(user: AuthUser, query: dict | None = None) -> dict:
    _require_hr(user)
    q = validate(MilestonesQueryIn, _q(query))
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
    r = Employee.objects.for_company(company_id).filter(pk=eid).values('contract_end', 'dob').first()
    if not r:
        raise NotFoundError('employee not found')
    p = relations_policy(company_id)
    dob = _iso(r['dob'])
    # The TS page adds the years to the date text (29 February stays 29 February).
    retirement = f"{int(dob[:4]) + p['retirementAge']}{dob[4:]}" if dob else None
    return {'contractEnd': _iso(r['contract_end']), 'retirementDate': retirement, 'retirementAge': p['retirementAge']}


def set_contract(user: AuthUser, employee_id: str, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    eid = parse_uuid(employee_id)
    b = validate(ContractIn, body)
    before = Employee.objects.for_company(company_id).filter(pk=eid).values('contract_end', 'join_date').first()
    if not before:
        raise NotFoundError('employee not found')
    join = _iso(before['join_date'])
    if b['contractEnd'] and join and b['contractEnd'] < join:
        raise BadRequestError('the contract cannot end before the join date')
    with transaction.atomic():
        Employee.objects.for_company(company_id).filter(pk=eid).update(contract_end=b['contractEnd'], updated_at=Now())
        audit('update', 'employee', actor_id=user.id, row_id=eid, before={'contractEnd': _iso(before['contract_end'])},
              after={'contractEnd': b['contractEnd']})
    return {'ok': True}


# ================================================================ acting and deputation

def assignments(company_id: str):
    """Acting and deputation assignments with the employee, branch and department names (dicts)."""
    return EmployeeTempAssignment.objects.for_company(company_id).values(
        'id', 'employee_id', 'kind', 'designation', 'branch_id', 'org_unit_id', 'start_date', 'end_date', 'note', 'status',
        'created_at', designation_id=F('designation_ref_id'), employee_name=F('employee__name_en'),
        emp_code=F('employee__emp_code'), branch_name=F('branch__name_en'), unit_name=F('org_unit__name_en'))


def _assignment_row(r: dict) -> dict:
    return _row(r, ('id', 'employee_id', 'designation_id', 'branch_id', 'org_unit_id'), ('start_date', 'end_date'))


def assignment_where(a: dict) -> str:
    if a['kind'] == TKind.ACTING:
        return a.get('designation') or ''
    return ', '.join(x for x in (a.get('branch_name'), a.get('unit_name')) if x)


def list_assignments(user: AuthUser, query: dict | None = None) -> dict:
    company_id = company_id_of(user)
    q = plain(validate(AssignmentsQueryIn, _q(query)))
    # HR sees everyone; anyone else only their own (managers see their team's on the directory card).
    if not is_hr(user) and (not q.get('employeeId') or q['employeeId'] != user.employee_id):
        raise ForbiddenError('HR manages acting and deputation assignments')
    rs = apply(AssignmentFilter, q, assignments(company_id))
    return {'assignments': [_assignment_row(r) for r in rs.order_by('-start_date')[:500]]}


def add_assignment(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = plain(validate(AssignmentIn, body))
    if b['endDate'] < b['startDate']:
        raise BadRequestError('the end date is before the start date')
    emp = _employee_in_company(company_id, b['employeeId'])
    if emp['status'] != ACTIVE:
        raise ConflictError('this employee has left')
    designation = b.get('designation') or None
    if b.get('designationId'):
        name = Designation.objects.for_company(company_id).filter(pk=b['designationId']).values_list('name_en', flat=True).first()
        if name is None:
            raise BadRequestError('designation does not exist in your company')
        designation = name
    for model, ref, label in ((Branch, b.get('branchId'), 'branch'), (OrgUnit, b.get('orgUnitId'), 'department')):
        if ref and not model.objects.for_company(company_id).filter(pk=ref).exists():
            raise BadRequestError(f'{label} does not exist in your company')
    if b['kind'] == TKind.ACTING and not designation:
        raise BadRequestError('choose the position they are acting in')
    if b['kind'] == TKind.DEPUTATION and not b.get('branchId') and not b.get('orgUnitId'):
        raise BadRequestError('choose where they are deputed to')
    from hajiri.people.career import log_career_event
    with transaction.atomic():
        # Inclusive date ranges overlap when each starts on or before the other ends.
        if (EmployeeTempAssignment.objects.for_company(company_id)
                .filter(employee_id=b['employeeId'], kind=b['kind'], status=TStatus.ACTIVE,
                        start_date__lte=b['endDate'], end_date__gte=b['startDate']).exists()):
            raise ConflictError(f"an {'acting assignment' if b['kind'] == TKind.ACTING else 'deputation'} already covers part of these dates")
        a = EmployeeTempAssignment.objects.create(
            company_id=company_id, employee_id=b['employeeId'], kind=b['kind'], designation=designation,
            designation_ref_id=b.get('designationId') or None, branch_id=b.get('branchId') or None,
            org_unit_id=b.get('orgUnitId') or None, start_date=b['startDate'], end_date=b['endDate'], note=b.get('note') or None,
            created_by_id=user.id)
        aid = str(a.id)
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
                   title=f'Acting as {designation}' if b['kind'] == TKind.ACTING else 'Deputation',
                   body=f"From {bs_label(b['startDate'])} to {bs_label(b['endDate'])}.", payload={'assignmentId': aid})
    return {'id': aid}


def end_assignment(user: AuthUser, id_: str, body: dict) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    b = validate(AssignmentEndIn, body or {})
    company_id = company_id_of(user)
    found = EmployeeTempAssignment.objects.for_company(company_id).filter(pk=id_)
    row = found.values('status', 'start_date').first()
    if not row:
        raise NotFoundError('assignment not found')
    if row['status'] != TStatus.ACTIVE:
        raise ConflictError(f"this assignment is {row['status']}")
    with transaction.atomic():
        if b['cancel']:
            found.update(status=TStatus.CANCELLED, ended_at=Now())
        else:
            end = b.get('endDate') or today()
            if end < _iso(row['start_date']):
                raise BadRequestError('the end date is before the start date; cancel it instead')
            # An end date already past ends the assignment now; one ahead only moves its end date.
            ended = end < today()
            found.update(end_date=end, status=TStatus.ENDED if ended else F('status'), ended_at=Now() if ended else None)
        after = {'endDate': b['endDate']} if 'endDate' in b else {}
        audit('cancel' if b['cancel'] else 'end', 'employee_temp_assignment', actor_id=user.id, row_id=id_, after=after)
    return {'ok': True}


# ================================================================ Know Your Employee

def _unit_and_below(company_id: str, unit_id: str | None) -> set[str]:
    """The org unit and every unit under it. A company has a few dozen units, so the tree is walked in Python."""
    if not unit_id:
        return set()
    children: dict[str, list[str]] = {}
    known = set()
    for u, parent in OrgUnit.objects.for_company(company_id).values_list('id', 'parent_id'):
        known.add(str(u))
        if parent:
            children.setdefault(str(parent), []).append(str(u))
    out: set[str] = set()
    todo = [str(unit_id)] if str(unit_id) in known else []
    while todo:
        u = todo.pop()
        if u not in out:
            out.add(u)
            todo += children.get(u, [])
    return out


def _audience_employees(company_id: str, kind: str, audience_id: str | None) -> list[str]:
    """Active employees a campaign reaches: everyone, or by the branch or department they are placed in today."""
    qs = Employee.objects.for_company(company_id).filter(status=ACTIVE)
    in_force = EmployeeAssignment.objects.filter(employee=OuterRef('pk'), valid__contains=npt_today())
    if kind == KyeCampaign.AudienceKind.BRANCH:
        qs = qs.filter(Exists(in_force.filter(branch_id=audience_id)))
    elif kind == KyeCampaign.AudienceKind.DEPARTMENT:
        qs = qs.filter(Exists(in_force.filter(org_unit_id__in=_unit_and_below(company_id, audience_id))))
    return [str(i) for i in qs.order_by('id').values_list('id', flat=True)]


def campaigns(company_id: str):
    return KyeCampaign.objects.for_company(company_id)


def list_campaigns(user: AuthUser) -> dict:
    _require_hr(user)
    rs = (campaigns(company_id_of(user))
          .annotate(total=Count('kye_responses'),
                    confirmed=Count('kye_responses', filter=Q(kye_responses__status=RStatus.CONFIRMED)),
                    changes_requested=Count('kye_responses', filter=Q(kye_responses__status=RStatus.CHANGES_REQUESTED)))
          .order_by('-created_at')
          .values('id', 'title', 'message', 'sections', 'audience_kind', 'audience_id', 'due_date', 'status', 'created_at',
                  'closed_at', 'total', 'confirmed', 'changes_requested'))
    return {'campaigns': [_row(r, ('id', 'audience_id'), ('due_date',)) for r in rs]}


def launch_campaign(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = plain(validate(CampaignIn, body))
    if b['dueDate'] < today():
        raise BadRequestError('the due date has already passed')
    kind = b['audienceKind']
    if kind != KyeCampaign.AudienceKind.ALL:
        model = Branch if kind == KyeCampaign.AudienceKind.BRANCH else OrgUnit
        if not b.get('audienceId') or not model.objects.for_company(company_id).filter(pk=b['audienceId']).exists():
            raise BadRequestError(f'choose the {kind}')
    audience_id = None if kind == KyeCampaign.AudienceKind.ALL else b.get('audienceId')
    with transaction.atomic():
        c = KyeCampaign.objects.create(company_id=company_id, title=b['title'], message=b.get('message') or None,
                                       sections=list(dict.fromkeys(b['sections'])), audience_kind=kind,
                                       audience_id=audience_id, due_date=b['dueDate'], created_by_id=user.id)
        cid = str(c.id)
        emps = _audience_employees(company_id, kind, audience_id)
        KyeResponse.objects.bulk_create([KyeResponse(campaign_id=cid, employee_id=e) for e in emps], ignore_conflicts=True)
        for e in emps:
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
    camp = (campaigns(company_id).filter(pk=id_)
            .values('id', 'title', 'message', 'sections', 'audience_kind', 'due_date', 'status').first())
    if not camp:
        raise NotFoundError('campaign not found')
    _row(camp, ('id',), ('due_date',))
    resp = (KyeResponse.objects.filter(campaign_id=id_)
            .order_by(_flag(Q(status=RStatus.PENDING)).desc(), 'employee__name_en')
            .values('employee_id', 'status', 'note', 'responded_at', 'last_nudged_at',
                    name_en=F('employee__name_en'), emp_code=F('employee__emp_code')))
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
    camp = campaigns(company_id).filter(pk=id_).values('title', 'due_date', 'status').first()
    if not camp:
        raise NotFoundError('campaign not found')
    if camp['status'] != KStatus.OPEN:
        raise ConflictError('this campaign is closed')
    sent = 0
    with transaction.atomic():
        # At most one nudge a day per person.
        due = (KyeResponse.objects.filter(campaign_id=id_, status=RStatus.PENDING)
               .filter(Q(last_nudged_at__isnull=True) | Q(last_nudged_at__lt=Now() - timedelta(hours=20))))
        pending = [str(e) for e in due.select_for_update().values_list('employee_id', flat=True)]
        KyeResponse.objects.filter(campaign_id=id_, employee_id__in=pending).update(last_nudged_at=Now())
        for eid in pending:
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
        if not campaigns(company_id_of(user)).filter(pk=id_, status=KStatus.OPEN).update(status=KStatus.CLOSED, closed_at=Now()):
            raise NotFoundError('open campaign not found')
        audit('close', 'kye_campaign', actor_id=user.id, row_id=id_)
    return {'ok': True}


def my_kye(user: AuthUser) -> dict:
    """The employee's open review requests and how complete their record is."""
    if not user.employee_id:
        return {'campaigns': [], 'completeness': None}
    company_id = company_id_of(user)
    rs = (KyeResponse.objects.filter(employee_id=user.employee_id, campaign__company_id=company_id, campaign__status=KStatus.OPEN)
          .order_by('campaign__due_date')
          .values('status', 'responded_at', id=F('campaign_id'), title=F('campaign__title'), message=F('campaign__message'),
                  sections=F('campaign__sections'), due_date=F('campaign__due_date')))
    comp = completeness(company_id, user.employee_id)
    return {'campaigns': [_row(r, ('id',), ('due_date',)) for r in rs], 'completeness': comp[0] if comp else None,
            'sectionLabels': SECTION_LABEL}


def respond_kye(user: AuthUser, id_: str, body: dict) -> dict:
    id_ = parse_uuid(id_)
    if not user.employee_id:
        raise _not_linked()
    b = validate(KyeResponseIn, body)
    with transaction.atomic():
        n = 0
        if campaigns(company_id_of(user)).select_for_update().filter(pk=id_, status=KStatus.OPEN).values_list('pk').first():
            n = KyeResponse.objects.filter(campaign_id=id_, employee_id=user.employee_id).update(
                status=b['status'], note=b.get('note') or None, responded_at=Now())
        if not n:
            raise NotFoundError('nothing to confirm for this campaign')
    return {'ok': True}


# ================================================================ sign-in pop-ups

def _popup_notices(user: AuthUser):
    """Pop-up notices that reach the person where they sit today (engage's audience rules)."""
    from hajiri.engage.audience import audience_q, placement_of
    return (Announcement.objects.for_company(company_id_of(user))
            .filter(audience_q(placement_of(user.employee_id), True), deleted_at__isnull=True, popup=True))


def popups(user: AuthUser) -> dict:
    t = npt_today()
    rs = (_popup_notices(user).exclude(author_id=user.id)
          .filter(Q(expires_on__isnull=True) | Q(expires_on__gte=t), Q(popup_until__isnull=True) | Q(popup_until__gte=t))
          .exclude(Exists(AnnouncementPopupSeen.objects.filter(announcement=OuterRef('pk'), user_id=user.id)))
          .exclude(Exists(AnnouncementAck.objects.filter(announcement=OuterRef('pk'), user_id=user.id)))
          .order_by('-created_at')
          .values('id', 'title', 'body', 'requires_ack', 'created_at',
                  author_name=Coalesce(F('author_employee__name_en'), F('author__name')))[:5])
    return {'popups': [{'id': str(x['id']), 'title': x['title'], 'body': x['body'], 'requiresAck': x['requires_ack'],
                        'createdAt': x['created_at'], 'authorName': x['author_name']} for x in rs]}


def dismiss_popup(user: AuthUser, id_: str, body: dict | None = None) -> dict:
    id_ = parse_uuid(id_)
    requires_ack = _popup_notices(user).filter(pk=id_).values_list('requires_ack', flat=True).first()
    if requires_ack is None:
        raise NotFoundError('notice not found')
    b = validate(DismissIn, body or {})
    if requires_ack and not b['acknowledge']:
        raise BadRequestError('confirm that you have read this notice')
    with transaction.atomic():
        AnnouncementPopupSeen.objects.bulk_create([AnnouncementPopupSeen(announcement_id=id_, user_id=user.id)],
                                                  ignore_conflicts=True)
        if requires_ack:
            AnnouncementAck.objects.bulk_create([AnnouncementAck(announcement_id=id_, user_id=user.id)], ignore_conflicts=True)
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
        if (RegisteredDevice.objects.for_company(company_id)
                .filter(token_hash=device_token_hash(token), employee_id=employee_id, status=DStatus.APPROVED)
                .update(last_used_at=Now())):
            return
    raise ForbiddenError(DEVICE_NOT_REGISTERED, {'code': 'device_not_registered'})


def devices_of(company_id: str):
    return RegisteredDevice.objects.for_company(company_id)


def my_devices(user: AuthUser) -> dict:
    if not user.employee_id:
        return {'devices': [], 'required': False}
    company_id = company_id_of(user)
    rs = (devices_of(company_id).filter(employee_id=user.employee_id).exclude(status=DStatus.REVOKED)
          .order_by('-requested_at').values('id', 'label', 'status', 'requested_at', 'decided_at', 'last_used_at'))
    return {'devices': [{**r, 'id': str(r['id'])} for r in rs], 'required': relations_policy(company_id)['registeredDevicesOnly']}


def register_device(user: AuthUser, body: dict, user_agent: str | None = None) -> dict:
    if not user.employee_id:
        raise _not_linked()
    company_id = company_id_of(user)
    b = validate(DeviceIn, body)
    if devices_of(company_id).filter(employee_id=user.employee_id, status__in=(DStatus.PENDING, DStatus.APPROVED)).count() >= 3:
        raise ConflictError('you already have three devices; remove one first')
    ua = (user_agent or '')[:200] or None
    token_hash = device_token_hash(b['token'])
    with transaction.atomic():
        # The token is unique across every company (registered_device.token_hash); a parallel insert of the same token
        # fails on the unique index and is answered the same way.
        if RegisteredDevice.objects.filter(token_hash=token_hash).exists():
            raise ConflictError('this device is already registered')
        try:
            with transaction.atomic():
                d = RegisteredDevice.objects.create(company_id=company_id, employee_id=user.employee_id, user_id=user.id,
                                                    token_hash=token_hash, label=b['label'], user_agent=ua)
        except IntegrityError:
            raise ConflictError('this device is already registered') from None
        did = str(d.id)
        for uid in user_ids_with_role(company_id, 'hr'):
            if uid == user.id:
                continue
            notify(company_id=company_id, user_id=uid, template='relations.device.new', title='New clock-in device to approve',
                   body=f'{user.name} registered "{b["label"]}".', payload={'deviceId': did})
    return {'id': did, 'status': DStatus.PENDING.value}


def remove_my_device(user: AuthUser, id_: str) -> dict:
    id_ = parse_uuid(id_)
    n = (devices_of(company_id_of(user)).filter(pk=id_, employee_id=user.employee_id).exclude(status=DStatus.REVOKED)
         .update(status=DStatus.REVOKED, decided_at=Now())) if user.employee_id else 0
    if not n:
        raise NotFoundError('device not found')
    return {'ok': True}


def list_devices(user: AuthUser, query: dict | None = None) -> dict:
    _require_hr(user)
    q = validate(DevicesQueryIn, _q(query))
    rs = (apply(DeviceFilter, q, devices_of(company_id_of(user))).order_by(_flag(Q(status=DStatus.PENDING)).desc(), '-requested_at')
          .values('id', 'employee_id', 'label', 'user_agent', 'status', 'requested_at', 'decided_at', 'last_used_at',
                  employee_name=F('employee__name_en'), emp_code=F('employee__emp_code'))[:500])
    return {'devices': [{**r, 'id': str(r['id']), 'employee_id': str(r['employee_id'])} for r in rs]}


def decide_device(user: AuthUser, id_: str, action: str) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    if action not in DEVICE_ACTIONS:
        raise ValidationError([{'path': '', 'message': choice_message(DEVICE_ACTIONS)}])
    company_id = company_id_of(user)
    nxt = {'approve': DStatus.APPROVED, 'reject': DStatus.REJECTED, 'revoke': DStatus.REVOKED}[action]
    if action == 'approve' and not has_role(user, 'admin') and user.employee_id:
        if devices_of(company_id).filter(pk=id_, employee_id=user.employee_id).exists():
            raise ForbiddenError('someone else must approve your own clock-in device')
    from_ = (DStatus.APPROVED, DStatus.PENDING) if action == 'revoke' else (DStatus.PENDING,)
    with transaction.atomic():
        u = devices_of(company_id).select_for_update().filter(pk=id_, status__in=from_).values('employee_id', 'label').first()
        if not u:
            raise ConflictError('this device cannot be changed that way')
        RegisteredDevice.objects.filter(pk=id_).update(status=nxt, decided_by_id=user.id, decided_at=Now())
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
    g = (Grievance.objects.for_company(company_id).filter(status__in=(*OPEN_GRIEVANCE, GStatus.APPEALED))
         .exclude(user_id=user.id).exclude(excluded_user_ids__contains=[user.id]).count())
    d = DisciplinaryCase.objects.for_company(company_id).filter(status__in=OPEN_CASE).count()
    dev = devices_of(company_id).filter(status=DStatus.PENDING).count()
    kye = campaigns(company_id).filter(status=KStatus.OPEN).count()
    return {'openGrievances': g, 'openCases': d, 'pendingDevices': dev, 'openKye': kye, 'isHr': has_role(user, 'hr')}


# ================================================================ daily job

def _manager_user_of(employee_id: str, on_date: str) -> str | None:
    """The account of the employee's line manager on that date (home account or group member)."""
    manager = (EmployeeAssignment.objects.filter(employee_id=employee_id, valid__contains=date.fromisoformat(on_date))
               .values_list('manager_id', flat=True).first())
    return user_id_for_employee(str(manager)) if manager else None


def relations_daily(now: datetime | None = None) -> dict:
    """
    Once a day: end acting and deputation assignments that ran out, remind HR (and the manager) about milestones, flag
    disciplinary responses that are overdue, remind KYE stragglers three days before the due date, and close decided
    grievances whose appeal window has passed. There is no active company here: each step works per row's company.
    """
    now = now or _now()
    day = npt_today(now)
    today_ = day.isoformat()
    out = {'assignmentsEnded': 0, 'milestoneReminders': 0, 'overdueResponses': 0, 'kyeReminders': 0, 'grievancesClosed': 0}

    with transaction.atomic():
        due = EmployeeTempAssignment.objects.filter(status=TStatus.ACTIVE, end_date__lt=day)
        ended = list(due.select_for_update().values('id', 'company_id', 'employee_id', 'kind'))
        EmployeeTempAssignment.objects.filter(pk__in=[t['id'] for t in ended]).update(status=TStatus.ENDED, ended_at=Now())
        for t in ended:
            out['assignmentsEnded'] += 1
            eid = str(t['employee_id'])
            uid = user_id_for_employee(eid)
            if uid:
                notify(company_id=str(t['company_id']), user_id=uid, employee_id=eid, template='relations.assignment.ended',
                       title='Acting assignment ended' if t['kind'] == TKind.ACTING else 'Deputation ended',
                       body='Your temporary assignment has ended; your regular role continues.', payload={'assignmentId': str(t['id'])})

    for cid in (str(c) for c in Company.objects.values_list('id', flat=True)):
        lst = milestones(cid, today_, 30)
        hr = user_ids_with_role(cid, 'hr')
        for m in lst:
            # Service anniversaries on the day; contract ends and retirements 30 and 7 days ahead and on the day.
            days_left = (date.fromisoformat(m['date']) - day).days
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
        overdue = DisciplinaryCase.objects.filter(status=CStatus.SHOW_CAUSE, response_due__lt=day, overdue_notified=False)
        flagged = list(overdue.select_for_update().values('id', 'company_id', 'number'))
        DisciplinaryCase.objects.filter(pk__in=[k['id'] for k in flagged]).update(overdue_notified=True)
        for k in flagged:
            for uid in user_ids_with_role(str(k['company_id']), 'hr'):
                notify(company_id=str(k['company_id']), user_id=uid, template='relations.disciplinary.overdue',
                       title=f"Case D-{k['number']}: no response yet", body='The response deadline has passed. You can now decide the case.',
                       payload={'caseId': str(k['id'])})
            out['overdueResponses'] += 1

    with transaction.atomic():
        closing = KyeCampaign.objects.filter(status=KStatus.OPEN, reminded=False, due_date__lte=day + timedelta(days=3))
        remind = list(closing.select_for_update().values('id', 'company_id', 'title', 'due_date'))
        KyeCampaign.objects.filter(pk__in=[k['id'] for k in remind]).update(reminded=True)
        for k in remind:
            pending = KyeResponse.objects.filter(campaign_id=k['id'], status=RStatus.PENDING).values_list('employee_id', flat=True)
            for e in pending:
                eid = str(e)
                uid = user_id_for_employee(eid)
                if uid:
                    notify(company_id=str(k['company_id']), user_id=uid, employee_id=eid, template='relations.kye.reminder',
                           title=f"Please review your profile: {k['title']}", body=f"Confirm your details by {bs_label(k['due_date'])}.",
                           payload={'campaignId': str(k['id'])})
                    out['kyeReminders'] += 1

    windows: dict[str, int] = {}
    for g in Grievance.objects.filter(status=GStatus.DECIDED, decided_at__isnull=False).values('id', 'company_id', 'decided_at'):
        cid = str(g['company_id'])
        if cid not in windows:
            windows[cid] = relations_policy(cid)['appealDays']
        if now - g['decided_at'] > timedelta(days=windows[cid]):
            Grievance.objects.filter(pk=g['id'], status=GStatus.DECIDED).update(status=GStatus.CLOSED, updated_at=Now())
            out['grievancesClosed'] += 1
    return out
