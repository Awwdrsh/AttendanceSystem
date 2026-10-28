"""
Help desk: employees raise questions and requests; HR (and named owners) answer them against an SLA.
Port of apps/api/src/modules/helpdesk/service.ts and routes.ts. Every public function takes the signed-in AuthUser
and a body in the TS API's shape (camelCase keys), validated by the serializers in serializers.py with the same rules
and messages as the zod schemas. Every query is scoped to the user's active company.
"""
from __future__ import annotations

import re
import uuid as _uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db import IntegrityError, models, transaction
from django.db.models import (
    Avg, Case, Count, DecimalField, ExpressionWrapper, F, IntegerField, Max, OuterRef, Q, Value, When,
)
from django.db.models.functions import Coalesce, Extract, Now

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.clock import now as clock_now
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.core.settings_store import add_setting_if_missing, get_setting, put_setting, setting_subquery
from hajiri.core.users import company_roles, company_users, user_ids_with_role
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import Company, HelpdeskCategory, HelpdeskMessage, HelpdeskTicket, StoredFile
from hajiri.files.store import attach_file, read_file
from hajiri.notifications.service import notify

from .serializers import (
    DEFAULT_SLA, PRIORITIES, STATUSES, CategoryIn, MessageIn, PatchIn, PolicyIn, QueueQueryIn, RateIn, TicketIn,
)

Status = HelpdeskTicket.Status
Priority = HelpdeskTicket.Priority
ACTIVE = (Status.OPEN, Status.IN_PROGRESS, Status.WAITING)
DONE = (Status.RESOLVED, Status.CLOSED)

PRIORITY_LABEL = {'low': 'Low', 'normal': 'Normal', 'high': 'High', 'urgent': 'Urgent'}
STATUS_LABEL = {'open': 'Open', 'in_progress': 'In progress', 'waiting': 'Waiting on you', 'resolved': 'Resolved',
                'closed': 'Closed'}

CATEGORIES = [
    ('Payroll & tax', 'Salary, payslips, deductions, TDS and SSF questions', False),
    ('Leave & attendance', 'Balances, missed punches, shifts and holidays', False),
    ('Documents & letters', 'Experience letters, salary certificates and other paperwork', False),
    ('IT & equipment', 'Laptop, email, access and other equipment', False),
    ('Other', 'Anything else for HR', False),
]

_TICKET_FIELDS = tuple(f.attname for f in HelpdeskTicket._meta.concrete_fields)
_PRIORITY_RANK = Case(When(priority=Priority.URGENT, then=0), When(priority=Priority.HIGH, then=1),
                      When(priority=Priority.NORMAL, then=2), default=3, output_field=IntegerField())


def _flag(q: Q) -> ExpressionWrapper:
    return ExpressionWrapper(q, output_field=models.BooleanField())


def tickets() -> models.QuerySet:
    """Tickets with the columns every view needs (TICKET_SELECT in the TS code)."""
    return HelpdeskTicket.objects.annotate(
        confidential=F('category__confidential'), owner_user_id=F('category__owner_user_id'),
        category_name=F('category__name'), requester_name=F('requester_user__name'),
        assignee_name=F('assignee_user__name'), emp_code=F('employee__emp_code'), employee_name=F('employee__name_en'))


def ticket_values(qs: models.QuerySet) -> models.QuerySet:
    """A tickets() queryset as dict rows (still lazy, so it can be paginated)."""
    return qs.values(*_TICKET_FIELDS, 'confidential', 'owner_user_id', 'category_name', 'requester_name',
                     'assignee_name', 'emp_code', 'employee_name')


def _rows(qs: models.QuerySet) -> list[dict]:
    """Ticket rows as dicts, UUIDs as strings (so they compare with AuthUser ids)."""
    return [plain(r) for r in ticket_values(qs)]


def _now() -> datetime:
    return clock_now()


# ---------------------------------------------------------------- policy and defaults

def helpdesk_policy(company_id: str) -> dict:
    value = get_setting(company_id, 'helpdesk.policy', {}) or {}
    try:
        return validate(PolicyIn, value if isinstance(value, dict) else {})
    except ValidationError:
        return validate(PolicyIn, {})


def ensure_helpdesk_defaults(company_id: str) -> bool:
    """The first time a company uses the help desk, add the standard topics (once: HR may delete or rename them)."""
    if not add_setting_if_missing(company_id, 'helpdesk.seeded', {'version': 1}):
        return False
    HelpdeskCategory.objects.bulk_create(
        [HelpdeskCategory(company_id=company_id, name=name, description=description, confidential=confidential, sort=i)
         for i, (name, description, confidential) in enumerate(CATEGORIES)], ignore_conflicts=True)
    return True


def _company(user: AuthUser) -> str:
    """companyIdOf(req) plus the router's ensureHelpdeskDefaults middleware."""
    company_id = company_id_of(user)
    ensure_helpdesk_defaults(company_id)
    return company_id


def _sla_due(company_id: str, priority: str, start: datetime) -> datetime:
    return start + timedelta(hours=helpdesk_policy(company_id)['slaHours'][priority])


# ---------------------------------------------------------------- who is who on a ticket

def role_on_ticket(user: AuthUser, t: dict) -> str | None:
    """
    HR handles every ticket; outside HR, the assignee and the category owner handle non-confidential tickets.
    Confidential tickets are only ever visible to HR and the person who raised them.
    """
    if user.company_id != str(t['company_id']):
        return None
    if has_role(user, 'hr'):
        return 'agent'
    if not t['confidential'] and (str(t.get('assignee_user_id')) == user.id or str(t.get('owner_user_id')) == user.id):
        return 'agent'
    if str(t['requester_user_id']) == user.id:
        return 'requester'
    return None


def handlers(t: dict) -> list[str]:
    """People to tell about something new on a ticket: the assignee, else the category owner, else HR."""
    if t.get('assignee_user_id'):
        return [str(t['assignee_user_id'])]
    if t.get('owner_user_id') and not t['confidential']:
        return [str(t['owner_user_id'])]
    return user_ids_with_role(str(t['company_id']), 'hr')


def _load_ticket(user: AuthUser, ticket_id: Any) -> tuple[dict, str]:
    tid = parse_uuid(ticket_id)
    found = _rows(tickets().for_company(company_id_of(user)).filter(pk=tid))
    t = found[0] if found else None
    role = role_on_ticket(user, t) if t else None
    if not t or not role:
        raise NotFoundError('ticket not found')
    return t, role


def _attach(company_id: str, user_id: str, message_id: str, ids: list[str]) -> None:
    """Only the uploader's own unattached help desk uploads are accepted."""
    for file_id in ids:
        attach_file(company_id=company_id, file_id=file_id, user_id=user_id, purpose='helpdesk_attachment',
                    attached_to=f'helpdesk_message:{message_id}')


def _within_reopen(t: dict, policy: dict) -> bool:
    return bool(t.get('resolved_at')) and (_now() - t['resolved_at']) <= timedelta(days=policy['reopenDays'])


def _is_hr(roles) -> bool:
    return any(x in ('hr', 'admin') for x in roles)


# ---------------------------------------------------------------- settings and categories

def get_settings(user: AuthUser) -> dict:
    return {'policy': helpdesk_policy(_company(user))}


def _require_hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


def put_settings(user: AuthUser, body: Any) -> dict:
    _require_hr(user)
    company_id = _company(user)
    policy = validate(PolicyIn, body)
    with transaction.atomic():
        put_setting(company_id, 'helpdesk.policy', policy, updated_by=user.id)
        audit('update', 'company_setting', actor_id=user.id, row_id='helpdesk.policy', after=policy)
    return {'policy': policy}


def list_categories(user: AuthUser) -> list[dict]:
    company_id = _company(user)
    hr = has_role(user, 'hr')
    qs = HelpdeskCategory.objects.for_company(company_id).order_by('sort', 'name')
    if not hr:
        qs = qs.filter(active=True)
    out = []
    for r in qs.values('id', 'name', 'description', 'confidential', 'sort', 'active', 'owner_user_id', 'owner_user__name'):
        owner_name = r.pop('owner_user__name')
        if not hr:                                  # owners are HR's business
            r['owner_user_id'], owner_name = None, None
        out.append(plain({**r, 'owner_name': owner_name}))
    return out


def _check_owner(company_id: str, owner_user_id: str | None, confidential: bool) -> None:
    if not owner_user_id:
        return
    roles = company_roles(company_id, owner_user_id)
    if roles is None:
        raise BadRequestError('the owner must be an active user in your company')
    if confidential and not _is_hr(roles):
        raise BadRequestError('a confidential category can only be owned by HR')


def _unique_name(fn):
    """A second category with the same name answers 409 with the TS message."""
    try:
        with transaction.atomic():
            return fn()
    except IntegrityError as e:
        if getattr(getattr(e, '__cause__', None), 'sqlstate', None) == '23505':
            raise ConflictError('a category with this name already exists') from e
        raise


def _category_fields(b: dict) -> dict:
    return {'name': b['name'], 'description': b.get('description'), 'confidential': b['confidential'],
            'owner_user_id': b.get('ownerUserId'), 'sort': b['sort'], 'active': b['active']}


def create_category(user: AuthUser, body: Any) -> dict:
    _require_hr(user)
    company_id = _company(user)
    b = plain(validate(CategoryIn, body))
    _check_owner(company_id, b.get('ownerUserId'), b['confidential'])
    c = _unique_name(lambda: HelpdeskCategory.objects.create(company_id=company_id, **_category_fields(b)))
    cid = str(c.id)
    audit('create', 'helpdesk_category', actor_id=user.id, row_id=cid, after=b)
    return {'id': cid}


def update_category(user: AuthUser, category_id: Any, body: Any) -> dict:
    _require_hr(user)
    company_id = _company(user)
    cid = parse_uuid(category_id)
    b = plain(validate(CategoryIn, body))
    _check_owner(company_id, b.get('ownerUserId'), b['confidential'])
    n = _unique_name(lambda: HelpdeskCategory.objects.for_company(company_id).filter(pk=cid).update(**_category_fields(b)))
    if not n:
        raise NotFoundError('category not found')
    audit('update', 'helpdesk_category', actor_id=user.id, row_id=cid, after=b)
    return {'ok': True}


def get_category(user: AuthUser, category_id: Any) -> dict:
    """One topic for HR's edit form."""
    _require_hr(user)
    company_id = _company(user)
    r = (HelpdeskCategory.objects.for_company(company_id).filter(pk=parse_uuid(category_id))
         .annotate(owner_name=F('owner_user__name'))
         .values('id', 'name', 'description', 'confidential', 'sort', 'active', 'owner_user_id', 'owner_name').first())
    if not r:
        raise NotFoundError('category not found')
    return plain(r)


def list_agents(user: AuthUser) -> list[dict]:
    """People HR can assign tickets to (HR for confidential categories): everyone active in the company, including
    people of the group who were given access to it."""
    _require_hr(user)
    return [plain({'id': u['id'], 'name': u['name'], 'email': u['email'], 'roles': u['roles'], 'is_hr': _is_hr(u['roles'])})
            for u in company_users(_company(user))]


# ---------------------------------------------------------------- tickets

def create_ticket(user: AuthUser, body: Any) -> dict:
    company_id = _company(user)
    b = plain(validate(TicketIn, body))
    c0 = (HelpdeskCategory.objects.for_company(company_id).filter(pk=b['categoryId'], active=True)
          .values('id', 'confidential', 'owner_user_id', 'name').first())
    if not c0:
        raise BadRequestError('choose a category')
    owner = c0['owner_user_id'] and str(c0['owner_user_id'])
    due = _sla_due(company_id, b['priority'], _now())
    assignee = None if c0['confidential'] else owner
    with transaction.atomic():
        # Ticket numbers are sequential per company: lock the company row (FOR NO KEY UPDATE, so rows that merely
        # reference the company are not blocked) while taking the next number.
        Company.objects.select_for_update(no_key=True).filter(pk=company_id).first()
        n = (HelpdeskTicket.objects.for_company(company_id).aggregate(n=Max('number'))['n'] or 0) + 1
        t = HelpdeskTicket.objects.create(company_id=company_id, number=n, requester_user_id=user.id,
                                          employee_id=user.employee_id, category_id=b['categoryId'], subject=b['subject'],
                                          priority=b['priority'], sla_due_at=due, assignee_user_id=assignee)
        tid = str(t.id)
        msg = HelpdeskMessage.objects.create(ticket=t, company_id=company_id, author_user_id=user.id, body=b['body'],
                                             file_ids=b['fileIds'])
        _attach(company_id, user.id, str(msg.id), b['fileIds'])
        targets = handlers({'company_id': company_id, 'assignee_user_id': assignee, 'owner_user_id': owner,
                            'confidential': c0['confidential']})
        for uid in targets:
            if uid == user.id:
                continue
            notify(company_id=company_id, user_id=uid, template='helpdesk.ticket.new', title=f"New ticket #{n}: {c0['name']}",
                   body='A confidential ticket was raised. Open the help desk to read it.' if c0['confidential'] else b['subject'],
                   payload={'ticketId': tid}, channels=('push',))
    return {'id': tid, 'number': n, 'slaDueAt': due}


def my_tickets(user: AuthUser) -> list[dict]:
    company_id = _company(user)
    qs = (tickets().for_company(company_id).filter(requester_user_id=user.id)
          .order_by(_flag(Q(status__in=DONE)), '-updated_at')[:200])
    out = _rows(qs)
    for r in out:
        r.pop('owner_user_id', None)
    return out


def queue_queryset(user: AuthUser, query: Any = None) -> models.QuerySet:
    """The help desk queue as a queryset (the page lists it; the JSON API paginates it). HR sees everything; others
    see the non-confidential tickets assigned to them or in categories they own."""
    company_id = _company(user)
    q = plain(validate(QueueQueryIn, dict(query or {})))
    overdue = Q(status__in=ACTIVE, sla_due_at__lt=Now())
    qs = tickets().for_company(company_id)
    if not has_role(user, 'hr'):
        qs = qs.filter(Q(category__confidential=False) & (Q(assignee_user_id=user.id) | Q(category__owner_user_id=user.id)))
    status = q.get('status')
    if status == 'active':
        qs = qs.filter(status__in=ACTIVE)
    elif status:
        qs = qs.filter(status=status)
    if q.get('categoryId'):
        qs = qs.filter(category_id=q['categoryId'])
    if q.get('priority'):
        qs = qs.filter(priority=q['priority'])
    assignee = user.id if q.get('assignee') == 'me' else q.get('assignee')
    if assignee == 'none':
        qs = qs.filter(assignee_user__isnull=True)
    elif assignee:
        qs = qs.filter(assignee_user_id=assignee)
    if q.get('breached'):
        qs = qs.filter(overdue)
    search = q['search'].strip() if q.get('search') is not None else None
    if search:
        # The TS query let a number search match every ticket (its subject/name test was skipped as "no search");
        # here "#12" or "12" finds ticket 12, as the search box promises.
        if re.fullmatch(r'#?\d+', search):
            qs = qs.filter(number=int(search.replace('#', '')))
        else:
            qs = qs.filter(Q(subject__icontains=search) | Q(requester_user__name__icontains=search))
    return qs.order_by(_flag(Q(status__in=DONE)), _flag(overdue).desc(), _PRIORITY_RANK, 'sla_due_at')


def queue(user: AuthUser, query: Any = None) -> list[dict]:
    return _rows(queue_queryset(user, query)[:500])


def _avg_hours(start: str, end: str, since: Q) -> Avg:
    """Average hours between two timestamps, as numeric (avg(extract(epoch from end - start) / 3600))."""
    epoch = Extract(ExpressionWrapper(F(end) - F(start), output_field=models.DurationField()), 'epoch')
    return Avg(ExpressionWrapper(epoch / Value(Decimal(3600)), output_field=DecimalField()), filter=since,
               output_field=DecimalField())


def _round_text(v, places: int) -> str | None:
    """round(numeric, n)::text"""
    if v is None:
        return None
    return str(Decimal(v).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def stats(user: AuthUser) -> dict:
    _require_hr(user)
    company_id = _company(user)
    now = Now()

    def since(n: int):
        return now - timedelta(days=n)

    s = HelpdeskTicket.objects.for_company(company_id).aggregate(
        open=Count('id', filter=Q(status__in=ACTIVE)),
        unassigned=Count('id', filter=Q(status=Status.OPEN, assignee_user__isnull=True)),
        overdue=Count('id', filter=Q(status__in=ACTIVE, sla_due_at__lt=now)),
        breached_90d=Count('id', filter=Q(breached_at__isnull=False, created_at__gt=since(90))),
        resolved_30d=Count('id', filter=Q(resolved_at__gt=since(30))),
        avg_resolution_hours=_avg_hours('created_at', 'resolved_at', Q(resolved_at__gt=since(90))),
        avg_first_response_hours=_avg_hours('created_at', 'first_response_at', Q(first_response_at__gt=since(90))),
        avg_rating=Avg('rating', filter=Q(rating__isnull=False, created_at__gt=since(180)), output_field=DecimalField()),
        ratings=Count('id', filter=Q(rating__isnull=False, created_at__gt=since(180))),
    )
    s['avg_resolution_hours'] = _round_text(s['avg_resolution_hours'], 1)
    s['avg_first_response_hours'] = _round_text(s['avg_first_response_hours'], 1)
    s['avg_rating'] = _round_text(s['avg_rating'], 2)
    by_cat = list(HelpdeskCategory.objects.for_company(company_id)
                  .annotate(open=Count('helpdesk_tickets', filter=Q(helpdesk_tickets__status__in=ACTIVE)),
                            total=Count('helpdesk_tickets'))
                  .order_by('sort').values('name', 'open', 'total'))
    return {'stats': s, 'byCategory': by_cat}


def ticket_detail(user: AuthUser, ticket_id: Any) -> dict:
    _company(user)
    t, role = _load_ticket(user, ticket_id)
    msgs = HelpdeskMessage.objects.filter(ticket_id=t['id']).order_by('created_at', 'id')   # uuidv7 ids break ties
    if role != 'agent':
        msgs = msgs.filter(internal=False)
    msgs = [plain(m) for m in msgs.annotate(author_name=F('author_user__name'), mine=_flag(Q(author_user_id=user.id)))
            .values('id', 'body', 'internal', 'file_ids', 'created_at', 'author_user_id', 'author_name', 'mine')]
    ids = list(dict.fromkeys(f for m in msgs for f in m['file_ids']))
    files = [plain(f) for f in StoredFile.objects.for_company(t['company_id']).filter(id__in=ids, deleted_at__isnull=True)
             .values('id', 'filename', 'mime', 'bytes')] if ids else []
    policy = helpdesk_policy(t['company_id'])
    can_reopen = role == 'requester' and t['status'] == Status.RESOLVED and _within_reopen(t, policy)
    ticket = dict(t)
    if role != 'agent':
        ticket.pop('owner_user_id', None)
    ticket['overdue'] = t['status'] in ACTIVE and t['sla_due_at'] < _now()
    return {'ticket': ticket, 'role': role, 'messages': msgs, 'files': files, 'canReopen': can_reopen}


def add_message(user: AuthUser, ticket_id: Any, body: Any) -> dict:
    _company(user)
    t, role = _load_ticket(user, ticket_id)
    b = plain(validate(MessageIn, body))
    if b['internal'] and role != 'agent':
        raise ForbiddenError('only the help desk team can add internal notes')
    if t['status'] == Status.CLOSED:
        raise ConflictError('this ticket is closed; raise a new ticket instead')
    policy = helpdesk_policy(t['company_id'])
    if t['status'] == Status.RESOLVED and role == 'requester' and not _within_reopen(t, policy):
        raise ConflictError('this ticket was resolved too long ago; raise a new ticket instead')
    this = HelpdeskTicket.objects.filter(pk=t['id'])
    with transaction.atomic():
        msg = HelpdeskMessage.objects.create(ticket_id=t['id'], company_id=t['company_id'], author_user_id=user.id,
                                             body=b['body'], internal=b['internal'], file_ids=b['fileIds'])
        mid = str(msg.id)
        _attach(t['company_id'], user.id, mid, b['fileIds'])
        if role == 'agent' and not b['internal']:
            # A public answer from the team counts as the first response and moves a new ticket along.
            this.update(first_response_at=Coalesce('first_response_at', Now()),
                        status=Case(When(status=Status.OPEN, then=Value(Status.IN_PROGRESS)), default=F('status'),
                                    output_field=models.TextField()),
                        updated_at=Now())
            if t['requester_user_id'] != user.id:
                notify(company_id=t['company_id'], user_id=t['requester_user_id'], template='helpdesk.ticket.reply',
                       title=f"Reply on ticket #{t['number']}", body=b['body'][:200], payload={'ticketId': t['id']},
                       channels=('push',))
        elif role == 'requester':
            # The requester answering (or writing again after a resolution) puts the ticket back in the team's hands.
            this.update(status=Case(When(status__in=(Status.WAITING, Status.RESOLVED), then=Value(Status.IN_PROGRESS)),
                                    default=F('status'), output_field=models.TextField()),
                        resolved_at=Case(When(status=Status.RESOLVED, then=Value(None)), default=F('resolved_at')),
                        updated_at=Now())
            for uid in handlers(t):
                if uid != user.id:
                    notify(company_id=t['company_id'], user_id=uid, template='helpdesk.ticket.reply',
                           title=f"New message on ticket #{t['number']}",
                           body='Open the help desk to read it.' if t['confidential'] else b['body'][:200],
                           payload={'ticketId': t['id']})
        else:
            this.update(updated_at=Now())
    return {'id': mid}


def update_ticket(user: AuthUser, ticket_id: Any, body: Any) -> dict:
    _company(user)
    t, role = _load_ticket(user, ticket_id)
    if role != 'agent':
        raise ForbiddenError('only the help desk team can change a ticket')
    b = plain(validate(PatchIn, body))
    hr = has_role(user, 'hr')
    if ('assigneeUserId' in b or 'categoryId' in b) and not hr:
        raise ForbiddenError('only HR can reassign or move a ticket')
    confidential = t['confidential']
    if b.get('categoryId'):
        c = (HelpdeskCategory.objects.for_company(t['company_id']).filter(pk=b['categoryId'])
             .values_list('confidential', flat=True).first())
        if c is None:
            raise BadRequestError('category not found')
        confidential = c
    new_assignee = b['assigneeUserId'] if 'assigneeUserId' in b else t['assignee_user_id']
    if new_assignee:
        roles = company_roles(t['company_id'], new_assignee)
        if roles is None:
            raise BadRequestError('the assignee must be an active user in your company')
        if confidential and not _is_hr(roles):
            raise BadRequestError('confidential tickets can only be assigned to HR')
        if new_assignee == t['requester_user_id']:
            raise BadRequestError('a ticket cannot be assigned to the person who raised it')
    priority = b.get('priority') or t['priority']
    due = _sla_due(t['company_id'], priority, t['created_at']) if b.get('priority') and b['priority'] != t['priority'] else None
    status = b.get('status')
    changes: dict[str, Any] = {'priority': priority, 'assignee_user_id': new_assignee, 'updated_at': Now()}
    if status:
        changes['status'] = status
        if status == Status.RESOLVED:
            # F() and When() see the row before this UPDATE, as the SQL CASE did.
            changes['resolved_at'] = Case(When(~Q(status=Status.RESOLVED), then=Now()), default=F('resolved_at'))
        elif status in ACTIVE:
            changes['resolved_at'] = None
        changes['closed_at'] = Now() if status == Status.CLOSED else None
    if b.get('categoryId'):
        changes['category_id'] = b['categoryId']
    if due is not None:
        changes['sla_due_at'] = due
        if due > _now():
            changes['breached_at'] = None
    with transaction.atomic():
        HelpdeskTicket.objects.filter(pk=t['id']).update(**changes)
        audit('update', 'helpdesk_ticket', actor_id=user.id, row_id=t['id'],
              before={'status': t['status'], 'priority': t['priority'], 'assignee': t['assignee_user_id']}, after=b)
        if b.get('assigneeUserId') and b['assigneeUserId'] != t['assignee_user_id'] and b['assigneeUserId'] != user.id:
            notify(company_id=t['company_id'], user_id=b['assigneeUserId'], template='helpdesk.ticket.assigned',
                   title=f"Ticket #{t['number']} assigned to you",
                   body='A confidential ticket. Open the help desk to read it.' if confidential else t['subject'],
                   payload={'ticketId': t['id']}, channels=('push',))
        if status == Status.RESOLVED and t['status'] != Status.RESOLVED and t['requester_user_id'] != user.id:
            notify(company_id=t['company_id'], user_id=t['requester_user_id'], template='helpdesk.ticket.resolved',
                   title=f"Ticket #{t['number']} resolved", body='Tell us how we did, or reply if you still need help.',
                   payload={'ticketId': t['id']}, channels=('push',))
    return {'ok': True}


def reopen_ticket(user: AuthUser, ticket_id: Any) -> dict:
    _company(user)
    t, role = _load_ticket(user, ticket_id)
    if role != 'requester' and not has_role(user, 'hr'):
        raise ForbiddenError('only the person who raised the ticket can reopen it')
    if t['status'] != Status.RESOLVED:
        raise ConflictError('this ticket is closed; raise a new ticket instead' if t['status'] == Status.CLOSED
                            else 'this ticket is still open')
    policy = helpdesk_policy(t['company_id'])
    if role == 'requester' and not _within_reopen(t, policy):
        raise ConflictError(f"tickets can be reopened within {policy['reopenDays']} days of being resolved")
    with transaction.atomic():
        HelpdeskTicket.objects.filter(pk=t['id']).update(status=Status.IN_PROGRESS, resolved_at=None, rating=None,
                                                         rating_comment=None, updated_at=Now())
        for uid in handlers(t):
            if uid != user.id:
                notify(company_id=t['company_id'], user_id=uid, template='helpdesk.ticket.reopened',
                       title=f"Ticket #{t['number']} reopened",
                       body='Open the help desk to read it.' if t['confidential'] else t['subject'], payload={'ticketId': t['id']})
        audit('reopen', 'helpdesk_ticket', actor_id=user.id, row_id=t['id'])
    return {'ok': True}


def close_ticket(user: AuthUser, ticket_id: Any) -> dict:
    _company(user)
    t, _role = _load_ticket(user, ticket_id)
    if t['status'] == Status.CLOSED:
        raise ConflictError('this ticket is already closed')
    with transaction.atomic():
        HelpdeskTicket.objects.filter(pk=t['id']).update(status=Status.CLOSED, closed_at=Now(),
                                                         resolved_at=Coalesce('resolved_at', Now()), updated_at=Now())
        audit('close', 'helpdesk_ticket', actor_id=user.id, row_id=t['id'])
    return {'ok': True}


def rate_ticket(user: AuthUser, ticket_id: Any, body: Any) -> dict:
    _company(user)
    t, _role = _load_ticket(user, ticket_id)
    if t['requester_user_id'] != user.id:
        raise ForbiddenError('only the person who raised the ticket can rate it')
    b = validate(RateIn, body)
    if t['status'] not in DONE:
        raise ConflictError('you can rate a ticket once it is resolved')
    if t['rating'] is not None:
        raise ConflictError('you have already rated this ticket')
    with transaction.atomic():
        HelpdeskTicket.objects.filter(pk=t['id']).update(rating=b['rating'], rating_comment=b.get('comment'),
                                                         updated_at=Now())
        if b['rating'] <= 2:
            targets = list(dict.fromkeys([*([t['assignee_user_id']] if t['assignee_user_id'] else []),
                                          *user_ids_with_role(t['company_id'], 'hr')]))
            for uid in targets:
                notify(company_id=t['company_id'], user_id=uid, template='helpdesk.ticket.low_rating',
                       title=f"Ticket #{t['number']} was rated {b['rating']} of 5",
                       body=b.get('comment') if b.get('comment') is not None else 'No comment.', payload={'ticketId': t['id']})
    return {'ok': True}


def ticket_file(user: AuthUser, ticket_id: Any, file_id: Any) -> dict:
    """An attachment of a message the user may see (internal notes' files only for the team)."""
    _company(user)
    t, role = _load_ticket(user, ticket_id)
    fid = parse_uuid(file_id)
    msgs = HelpdeskMessage.objects.filter(ticket_id=t['id'], file_ids__contains=[_uuid.UUID(fid)])
    if role != 'agent':
        msgs = msgs.filter(internal=False)
    if not msgs.exists():
        raise NotFoundError('file not found')
    return read_file(t['company_id'], fid)


# ---------------------------------------------------------------- daily job

def helpdesk_daily(now: datetime | None = None) -> dict:
    """Close tickets resolved long enough ago, and escalate tickets that passed their SLA without being resolved."""
    at = now or _now()
    with transaction.atomic():
        closed = 0
        companies = Company.objects.annotate(days=Coalesce(setting_subquery(
            'helpdesk.policy', 'autoCloseDays', company_ref=OuterRef('pk'), output_field=IntegerField()), 7))
        for c in companies:
            closed += (HelpdeskTicket.objects.for_company(c.pk)
                       .filter(status=Status.RESOLVED, resolved_at__lt=at - timedelta(days=c.days))
                       .update(status=Status.CLOSED, closed_at=at, updated_at=at))
        due = (tickets().select_for_update(of=('self',))
               .filter(status__in=ACTIVE, breached_at__isnull=True, sla_due_at__lt=at))
        breached = _rows(due)
        HelpdeskTicket.objects.filter(pk__in=[t['id'] for t in breached]).update(breached_at=at)
        for t in breached:
            targets = list(dict.fromkeys([*handlers(t), *user_ids_with_role(t['company_id'], 'hr')]))
            what = 'A confidential ticket' if t['confidential'] else f'"{t["subject"]}"'
            for uid in targets:
                notify(company_id=t['company_id'], user_id=uid, template='helpdesk.sla_breached',
                       title=f"Ticket #{t['number']} is past its due time",
                       body=f"{what} ({t['priority']} priority) has not been resolved in time.",
                       payload={'ticketId': t['id']}, channels=('push',))
    return {'ticketsClosed': closed, 'ticketsBreached': len(breached)}


__all__ = [
    'ACTIVE', 'CATEGORIES', 'DEFAULT_SLA', 'PRIORITIES', 'PRIORITY_LABEL', 'STATUSES', 'STATUS_LABEL',
    'add_message', 'close_ticket', 'create_category', 'create_ticket', 'ensure_helpdesk_defaults', 'get_category',
    'get_settings', 'handlers', 'helpdesk_daily', 'helpdesk_policy', 'list_agents', 'list_categories', 'my_tickets',
    'put_settings', 'queue', 'queue_queryset', 'rate_ticket', 'reopen_ticket', 'role_on_ticket', 'stats', 'tickets',
    'ticket_detail', 'ticket_file', 'ticket_values', 'update_category', 'update_ticket',
]
