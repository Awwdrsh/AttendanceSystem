"""
The manager's journal, continuous feedback and 1-on-1s (port of apps/api/src/modules/performance/conversations.ts).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import BooleanField, Case, Count, ExpressionWrapper, F, Q, Value, When
from django.db.models.functions import Coalesce, Now

from hajiri.core.auth import AuthUser, company_id_of, has_role, is_manager_of
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.scoping import as_uuid
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import Employee, PerfFeedback, PerfJournal, PerfOneOnOne, PerfOneOnOneItem
from hajiri.notifications.service import notify, user_id_for_employee

from . import serializers as s
from .common import current_assignments, direct_manager, employee_in_company, is_above_in_chain, me, plain_row, plain_rows

CADENCE_DAYS = {PerfOneOnOne.Cadence.WEEKLY: 7, PerfOneOnOne.Cadence.BIWEEKLY: 14, PerfOneOnOne.Cadence.MONTHLY: 30}
CADENCES = s.CADENCES
TAGS = s.TAGS
_JOURNAL_ONLY = "only the employee's managers and HR keep a journal"
FStatus, MStatus, Kind = PerfFeedback.Status, PerfOneOnOne.Status, PerfOneOnOneItem.Kind


def _instant(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace('Z', '+00:00'))


# ---------------------------------------------------------------- manager's journal (private to the author and HR)

def journal(user: AuthUser, query: dict) -> list[dict]:
    q = plain(validate(s.JournalQueryIn, query))
    company_id = company_id_of(user)
    employee_in_company(company_id, q['employeeId'])
    if not is_above_in_chain(user, q['employeeId']):
        raise ForbiddenError(_JOURNAL_ONLY)
    qs = PerfJournal.objects.for_company(company_id).filter(employee_id=q['employeeId'])
    if not has_role(user, 'hr'):
        qs = qs.filter(author_id=user.id)
    if q.get('from'):
        qs = qs.filter(note_date__gte=q['from'])
    if q.get('to'):
        qs = qs.filter(note_date__lte=q['to'])
    return plain_rows(qs.order_by('-note_date', '-created_at')
                      .values('id', 'note_date', 'tag', 'note', 'created_at', 'author_id', author_name=F('author__name')))


def add_journal(user: AuthUser, body: dict) -> str:
    b = plain(validate(s.JournalIn, body))
    company_id = company_id_of(user)
    employee_in_company(company_id, b['employeeId'])
    if not is_above_in_chain(user, b['employeeId']):
        raise ForbiddenError(_JOURNAL_ONLY)
    j = PerfJournal.objects.create(company_id=company_id, employee_id=b['employeeId'], author_id=user.id, note_date=b['noteDate'],
                                   tag=b['tag'], note=b['note'])
    return str(j.id)


def delete_journal(user: AuthUser, entry_id: str) -> None:
    n, _ = PerfJournal.objects.for_company(company_id_of(user)).filter(pk=parse_uuid(entry_id), author_id=user.id).delete()
    if not n:
        raise NotFoundError('note not found')


# ---------------------------------------------------------------- continuous feedback

FEEDBACK_VALUES = ('id', 'status', 'prompt', 'body', 'private_to_manager', 'created_at', 'given_at', 'giver_employee_id',
                   'subject_employee_id', 'requester_employee_id')


def _feedback_rows(qs) -> list[dict]:
    return plain_rows(qs.values(*FEEDBACK_VALUES, giver_name=F('giver_employee__name_en'), subject_name=F('subject_employee__name_en'),
                                requester_name=F('requester_employee__name_en')))


def feedback(user: AuthUser, query: dict) -> list[dict]:
    """
    received: given feedback about me (not the private-to-manager ones). given: what I wrote. requests: feedback I was
    asked for and requests I made. about: everything about an employee, for their managers and HR.
    """
    q = plain(validate(s.FeedbackQueryIn, query))
    company_id = company_id_of(user)
    qs = PerfFeedback.objects.for_company(company_id)
    if q['box'] == 'about':
        if not q.get('employeeId'):
            raise BadRequestError('employeeId is required')
        employee_in_company(company_id, q['employeeId'])
        if not is_above_in_chain(user, q['employeeId']):
            raise ForbiddenError("only the employee's managers and HR see all feedback")
        return _feedback_rows(qs.filter(subject_employee_id=q['employeeId'], status=FStatus.GIVEN).order_by('-given_at'))
    emp = me(user)
    where = {
        'received': Q(subject_employee_id=emp, status=FStatus.GIVEN, private_to_manager=False),
        'given': Q(giver_employee_id=emp, status=FStatus.GIVEN),
        'requests': Q(giver_employee_id=emp, status=FStatus.REQUESTED) | Q(requester_employee_id=emp),
    }[q['box']]
    out = []
    for f in _feedback_rows(qs.filter(where).order_by(Coalesce('given_at', 'created_at').desc())[:300]):
        # A requester who is the subject sees who answered, but not the text of private-to-manager answers.
        if q['box'] == 'requests' and f['giver_employee_id'] != emp and f['private_to_manager'] and f['subject_employee_id'] == emp:
            f = {**f, 'body': None}
        out.append(f)
    return out


def give_feedback(user: AuthUser, body: dict) -> None:
    b = plain(validate(s.GiveFeedbackIn, body))
    giver = me(user)
    company_id = company_id_of(user)
    if b['toEmployeeId'] == giver:
        raise BadRequestError('you cannot give feedback to yourself')
    employee_in_company(company_id, b['toEmployeeId'])
    with transaction.atomic():
        PerfFeedback.objects.create(company_id=company_id, giver_employee_id=giver, subject_employee_id=b['toEmployeeId'],
                                    status=FStatus.GIVEN, body=b['body'], private_to_manager=b['privateToManager'], given_at=Now())
        to = direct_manager(b['toEmployeeId']) if b['privateToManager'] else b['toEmployeeId']
        if to:
            notify(company_id=company_id, user_id=user_id_for_employee(to), employee_id=to, template='feedback.given', title='New feedback',
                   body=f'{user.name} shared feedback about a member of your team.' if b['privateToManager'] else f'{user.name} gave you feedback.',
                   payload={})


def request_feedback(user: AuthUser, body: dict) -> None:
    b = plain(validate(s.RequestFeedbackIn, body))
    requester = me(user)
    company_id = company_id_of(user)
    subject = b.get('aboutEmployeeId') or requester
    employee_in_company(company_id, subject)
    if subject != requester and not is_above_in_chain(user, subject):
        raise ForbiddenError('you can ask for feedback about yourself or your team')
    prompt = b.get('prompt')
    with transaction.atomic():
        for frm in dict.fromkeys(b['fromEmployeeIds']):
            if frm == subject:
                raise BadRequestError('someone cannot give feedback about themselves')
            employee_in_company(company_id, frm)
            PerfFeedback.objects.create(company_id=company_id, giver_employee_id=frm, subject_employee_id=subject,
                                        requester_employee_id=requester, status=FStatus.REQUESTED, prompt=prompt)
            notify(company_id=company_id, user_id=user_id_for_employee(frm), employee_id=frm, template='feedback.requested',
                   title='Feedback requested',
                   body=f"{user.name} asked for your feedback{' about a colleague' if subject != requester else ''}"
                        f"{': ' + prompt[:140] if prompt else '.'}", payload={})


def _open_request(company_id: str, fid: str, giver: str) -> dict:
    r = plain_row(PerfFeedback.objects.for_company(company_id).filter(pk=fid, giver_employee_id=giver)
                  .values('id', 'status', 'requester_employee_id', 'subject_employee_id').first())
    if not r:
        raise NotFoundError('feedback request not found')
    if r['status'] != FStatus.REQUESTED:
        raise ConflictError('this request was already answered')
    return r


def answer_feedback(user: AuthUser, fid: str, body: dict) -> None:
    b = validate(s.AnswerFeedbackIn, body)
    giver = me(user)
    company_id = company_id_of(user)
    f = _open_request(company_id, parse_uuid(fid), giver)
    with transaction.atomic():
        PerfFeedback.objects.filter(pk=f['id']).update(status=FStatus.GIVEN, body=b['body'], private_to_manager=b['privateToManager'],
                                                       given_at=Now())
        if f['requester_employee_id']:
            r = f['requester_employee_id']
            notify(company_id=company_id, user_id=user_id_for_employee(r), employee_id=r, template='feedback.answered', title='Feedback received',
                   body=f'{user.name} answered your feedback request.', payload={})


def decline_feedback(user: AuthUser, fid: str) -> None:
    giver = me(user)
    f = _open_request(company_id_of(user), parse_uuid(fid), giver)
    PerfFeedback.objects.filter(pk=f['id']).update(status=FStatus.DECLINED)


def get_feedback_request(user: AuthUser, fid: str) -> dict:
    """An open request I was asked to answer (for the answer form)."""
    emp = me(user)
    rows = _feedback_rows(PerfFeedback.objects.for_company(company_id_of(user))
                          .filter(pk=parse_uuid(fid), giver_employee_id=emp, status=FStatus.REQUESTED))
    if not rows:
        raise NotFoundError('feedback request not found')
    return rows[0]


# ---------------------------------------------------------------- 1-on-1s (only the two people in the meeting)

def _load_meeting(company_id: str, mid: str, employee_id: str, lock: bool = False) -> dict:
    qs = PerfOneOnOne.objects.for_company(company_id).filter(pk=as_uuid(mid))
    if lock:
        qs = qs.select_for_update()
    m = plain_row(qs.values('id', 'company_id', 'manager_employee_id', 'employee_id', 'scheduled_at', 'cadence', 'status').first())
    if not m or (m['manager_employee_id'] != employee_id and m['employee_id'] != employee_id):
        raise NotFoundError('1-on-1 not found')
    return {**m, 'side': 'manager' if m['manager_employee_id'] == employee_id else 'employee'}


def meetings(user: AuthUser, with_: str | None = None) -> dict:
    w = parse_uuid(with_) if with_ else None
    emp = me(user)
    company_id = company_id_of(user)
    qs = PerfOneOnOne.objects.for_company(company_id).filter(Q(manager_employee_id=emp) | Q(employee_id=emp))
    if w:
        qs = qs.filter(Q(manager_employee_id=w) | Q(employee_id=w))
    scheduled = Q(status=MStatus.SCHEDULED)
    ms = (qs.annotate(side=Case(When(manager_employee_id=emp, then=Value('manager')), default=Value('employee')),
                      open_actions=Count('perf_one_on_one_items', filter=Q(perf_one_on_one_items__kind=Kind.ACTION,
                                                                           perf_one_on_one_items__done=False)),
                      agenda_items=Count('perf_one_on_one_items', filter=Q(perf_one_on_one_items__kind=Kind.AGENDA)))
          .order_by(ExpressionWrapper(scheduled, output_field=BooleanField()).desc(),
                    Case(When(scheduled, then=F('scheduled_at'))).asc(), '-scheduled_at')
          .values('id', 'manager_employee_id', 'employee_id', 'scheduled_at', 'cadence', 'status', 'side', 'open_actions', 'agenda_items',
                  manager_name=F('manager_employee__name_en'), employee_name=F('employee__name_en'))[:200])
    # Who I can meet with: my direct reports and my own manager.
    report_ids = current_assignments().filter(manager_id=emp).values('employee_id')
    reports = (Employee.objects.for_company(company_id).filter(pk__in=report_ids, status=Employee.Status.ACTIVE)
               .order_by('name_en').values('id', 'name_en', 'designation'))
    mgr = direct_manager(emp)
    manager = Employee.objects.for_company(company_id).filter(pk=mgr).values('id', 'name_en', 'designation').first() if mgr else None
    return {'meetings': plain_rows(ms), 'reports': plain_rows(reports), 'manager': plain_row(manager)}


def create_meeting(user: AuthUser, body: dict) -> str:
    b = plain(validate(s.MeetingIn, body))
    emp = me(user)
    company_id = company_id_of(user)
    employee_in_company(company_id, b['withEmployeeId'])
    if is_manager_of(emp, b['withEmployeeId']):
        manager_id, employee_id = emp, b['withEmployeeId']
    elif is_manager_of(b['withEmployeeId'], emp):
        manager_id, employee_id = b['withEmployeeId'], emp
    else:
        raise ForbiddenError('1-on-1s are between a person and their direct manager')
    with transaction.atomic():
        m = PerfOneOnOne.objects.create(company_id=company_id, manager_employee_id=manager_id, employee_id=employee_id,
                                        scheduled_at=_instant(b['scheduledAt']), cadence=b['cadence'], created_by_id=user.id)
        mid = str(m.id)
        PerfOneOnOneItem.objects.bulk_create([PerfOneOnOneItem(meeting_id=mid, kind=Kind.AGENDA, text=t, added_by_id=user.id)
                                              for t in b['agenda']])
        other = b['withEmployeeId']
        notify(company_id=company_id, user_id=user_id_for_employee(other), employee_id=other, template='one_on_one.scheduled',
               title='1-on-1 scheduled', body=f'{user.name} scheduled a 1-on-1 with you.', payload={'meetingId': mid})
    return mid


MEETING_VALUES = ('id', 'company_id', 'manager_employee_id', 'employee_id', 'scheduled_at', 'cadence', 'status', 'shared_notes',
                  'created_by_id', 'created_at')


def meeting_detail(user: AuthUser, mid: str) -> dict:
    emp = me(user)
    company_id = company_id_of(user)
    m = _load_meeting(company_id, parse_uuid(mid), emp)
    private = 'manager_private_notes' if m['side'] == 'manager' else 'employee_private_notes'
    row = plain_row(PerfOneOnOne.objects.filter(pk=m['id'])
                    .values(*MEETING_VALUES, private_notes=F(private), manager_name=F('manager_employee__name_en'),
                            employee_name=F('employee__name_en')).get())
    items = (PerfOneOnOneItem.objects.filter(meeting_id=m['id']).order_by('kind', 'created_at')
             .values('id', 'kind', 'text', 'owner_employee_id', 'done', 'created_at', 'added_by_id', owner_name=F('owner_employee__name_en')))
    # Earlier open action items between the same two people, so nothing agreed is forgotten.
    carried = (PerfOneOnOneItem.objects.for_company(company_id)
               .filter(meeting__manager_employee_id=m['manager_employee_id'], meeting__employee_id=m['employee_id'], kind=Kind.ACTION,
                       done=False).exclude(meeting_id=m['id']).order_by('meeting__scheduled_at')
               .values('id', 'text', 'owner_employee_id', owner_name=F('owner_employee__name_en'), scheduled_at=F('meeting__scheduled_at')))
    return {'meeting': {**row, 'side': m['side']}, 'items': plain_rows(items), 'openActions': plain_rows(carried)}


def update_meeting(user: AuthUser, mid: str, body: dict) -> str | None:
    """Returns the id of the next meeting when a recurring one is marked done."""
    b = validate(s.MeetingPatch, body)
    emp = me(user)
    company_id = company_id_of(user)
    with transaction.atomic():
        m = _load_meeting(company_id, parse_uuid(mid), emp, lock=True)   # marking it done twice must not book two next ones
        changes = {}
        if 'scheduledAt' in b:
            changes['scheduled_at'] = _instant(b['scheduledAt'])
        if 'cadence' in b:
            changes['cadence'] = b['cadence']
        if 'sharedNotes' in b:
            changes['shared_notes'] = b['sharedNotes']
        if 'privateNotes' in b:
            changes['manager_private_notes' if m['side'] == 'manager' else 'employee_private_notes'] = b['privateNotes']
        if 'status' in b:
            changes['status'] = b['status']
        if changes:
            PerfOneOnOne.objects.filter(pk=m['id']).update(**changes)
        cadence = b.get('cadence') or m['cadence']
        if b.get('status') == MStatus.DONE and m['status'] != MStatus.DONE and CADENCE_DAYS.get(cadence):
            base = changes.get('scheduled_at') or m['scheduled_at']
            nxt = PerfOneOnOne.objects.create(company_id=company_id, manager_employee_id=m['manager_employee_id'], employee_id=m['employee_id'],
                                              scheduled_at=base + timedelta(days=CADENCE_DAYS[cadence]), cadence=cadence,
                                              created_by_id=user.id)
            return str(nxt.id)
    return None


def add_meeting_item(user: AuthUser, mid: str, body: dict) -> str:
    b = plain(validate(s.MeetingItemIn, body))
    emp = me(user)
    m = _load_meeting(company_id_of(user), parse_uuid(mid), emp)
    owner = b.get('ownerEmployeeId')
    if owner and owner not in (m['manager_employee_id'], m['employee_id']):
        raise BadRequestError('the owner must be one of the two people in the meeting')
    item = PerfOneOnOneItem.objects.create(meeting_id=m['id'], kind=b['kind'], text=b['text'], owner_employee_id=owner, added_by_id=user.id)
    return str(item.id)


def _item_meeting(company_id: str, item_id: str, emp: str) -> dict:
    meeting_id = PerfOneOnOneItem.objects.for_company(company_id).filter(pk=item_id).values_list('meeting_id', flat=True).first()
    if not meeting_id:
        raise NotFoundError('item not found')
    return _load_meeting(company_id, str(meeting_id), emp)


def update_meeting_item(user: AuthUser, item_id: str, body: dict) -> dict:
    b = validate(s.MeetingItemPatch, body)
    item_id = parse_uuid(item_id)
    m = _item_meeting(company_id_of(user), item_id, me(user))
    changes = {k: b[k] for k in ('done', 'text') if b.get(k) is not None}
    if changes:
        PerfOneOnOneItem.objects.filter(pk=item_id).update(**changes)
    return m


def delete_meeting_item(user: AuthUser, item_id: str) -> dict:
    item_id = parse_uuid(item_id)
    m = _item_meeting(company_id_of(user), item_id, me(user))
    PerfOneOnOneItem.objects.filter(pk=item_id).delete()
    return m
