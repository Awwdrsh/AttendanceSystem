"""
Help desk: employees raise questions and requests; HR (and named owners) answer them against an SLA.
Port of apps/api/src/modules/helpdesk/service.ts and routes.ts. Every public function takes the signed-in AuthUser
and a body in the TS API's shape (camelCase keys), validated with the same rules and messages as the zod schemas.
"""
from __future__ import annotations

import json
import re
import uuid as _uuid
from datetime import datetime, timedelta
from typing import Any

from django.db import IntegrityError, transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.clock import now as clock_now
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import Arr, Bool, Enum, Int, Obj, Str, Union, Uuid, parse, parse_uuid, safe_parse
from hajiri.files.store import attach_file, read_file
from hajiri.notifications.service import notify, user_ids_with_role

PRIORITIES = ('low', 'normal', 'high', 'urgent')
STATUSES = ('open', 'in_progress', 'waiting', 'resolved', 'closed')
ACTIVE = ('open', 'in_progress', 'waiting')

PRIORITY_LABEL = {'low': 'Low', 'normal': 'Normal', 'high': 'High', 'urgent': 'Urgent'}
STATUS_LABEL = {'open': 'Open', 'in_progress': 'In progress', 'waiting': 'Waiting on you', 'resolved': 'Resolved',
                'closed': 'Closed'}

DEFAULT_SLA = {'low': 72, 'normal': 48, 'high': 24, 'urgent': 8}
_HOURS = Int(min=1, max=720)
POLICY_SCHEMA = Obj({
    # Hours from raising a ticket to resolving it, by priority.
    'slaHours': Obj({'low': _HOURS, 'normal': _HOURS, 'high': _HOURS, 'urgent': _HOURS}, default=lambda: dict(DEFAULT_SLA)),
    # Resolved tickets close by themselves after this many days.
    'autoCloseDays': Int(min=1, max=60, default=7),
    # The requester can reopen a resolved ticket within this many days.
    'reopenDays': Int(min=0, max=60, default=7),
})

CATEGORIES = [
    ('Payroll & tax', 'Salary, payslips, deductions, TDS and SSF questions', False),
    ('Leave & attendance', 'Balances, missed punches, shifts and holidays', False),
    ('Documents & letters', 'Experience letters, salary certificates and other paperwork', False),
    ('IT & equipment', 'Laptop, email, access and other equipment', False),
    ('Other', 'Anything else for HR', False),
]

FILE_IDS = Arr(Uuid(), max=5, default=list)

CATEGORY_BODY = Obj({
    'name': Str(trim=True, min=1, max=80), 'description': Str(max=300, nullish=True),
    'confidential': Bool(default=False), 'ownerUserId': Uuid(nullish=True), 'sort': Int(min=0, max=999, default=0),
    'active': Bool(default=True),
})

TICKET_BODY = Obj({
    'categoryId': Uuid(), 'subject': Str(trim=True, min=3, max=160), 'body': Str(trim=True, min=1, max=5000),
    'priority': Enum(PRIORITIES, default='normal'), 'fileIds': FILE_IDS,
})

QUEUE_QUERY = Obj({
    'status': Enum((*STATUSES, 'active'), optional=True), 'categoryId': Uuid(optional=True),
    'priority': Enum(PRIORITIES, optional=True),
    'assignee': Union(Uuid(), Enum(('me',)), Enum(('none',)), optional=True),
    'breached': Enum(('1', 'true'), optional=True), 'search': Str(max=80, optional=True),
})

MESSAGE_BODY = Obj({'body': Str(trim=True, min=1, max=5000), 'internal': Bool(default=False), 'fileIds': FILE_IDS})

PATCH_BODY = Obj({
    'status': Enum(STATUSES, optional=True), 'priority': Enum(PRIORITIES, optional=True),
    'assigneeUserId': Uuid(nullable=True, optional=True), 'categoryId': Uuid(optional=True),
})

RATE_BODY = Obj({'rating': Int(min=1, max=5), 'comment': Str(trim=True, max=1000, nullish=True)})

TICKET_SELECT = """SELECT t.*, c.confidential, c.owner_user_id, c.name AS category_name,
    ru.name AS requester_name, au.name AS assignee_name, e.emp_code, e.name_en AS employee_name
  FROM helpdesk_ticket t JOIN helpdesk_category c ON c.id = t.category_id JOIN app_user ru ON ru.id = t.requester_user_id
  LEFT JOIN app_user au ON au.id = t.assignee_user_id LEFT JOIN employee e ON e.id = t.employee_id"""

_ID_KEYS = ('id', 'company_id', 'requester_user_id', 'employee_id', 'category_id', 'assignee_user_id', 'owner_user_id',
            'author_user_id', 'ticket_id')


def _norm(r: dict | None) -> dict | None:
    """UUID columns as strings, so they compare with AuthUser ids."""
    if r is None:
        return None
    for k in _ID_KEYS:
        if isinstance(r.get(k), _uuid.UUID):
            r[k] = str(r[k])
    if 'file_ids' in r:
        r['file_ids'] = [str(x) for x in (r['file_ids'] or [])]
    return r


def _now() -> datetime:
    return clock_now()


# ---------------------------------------------------------------- policy and defaults

def helpdesk_policy(company_id: str) -> dict:
    r = one("SELECT value FROM company_setting WHERE company_id = %s AND key = 'helpdesk.policy'", [company_id])
    value = r['value'] if r and r['value'] is not None else {}
    ok, parsed = safe_parse(POLICY_SCHEMA, value)
    return parsed if ok else parse(POLICY_SCHEMA, {})


def ensure_helpdesk_defaults(company_id: str) -> bool:
    """The first time a company uses the help desk, add the standard topics (once: HR may delete or rename them)."""
    n = execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'helpdesk.seeded', '{"version":1}')
                   ON CONFLICT DO NOTHING""", [company_id])
    if n == 0:
        return False
    for i, (name, description, confidential) in enumerate(CATEGORIES):
        execute("""INSERT INTO helpdesk_category (company_id, name, description, confidential, sort)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""", [company_id, name, description, confidential, i])
    return True


def _company(user: AuthUser) -> str:
    """companyIdOf(req) plus the router's ensureHelpdeskDefaults middleware."""
    company_id = company_id_of(user)
    ensure_helpdesk_defaults(company_id)
    return company_id


def _sla_due(company_id: str, priority: str, start: datetime) -> datetime:
    p = helpdesk_policy(company_id)
    return start + timedelta(hours=p['slaHours'][priority])


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
    tid = parse_uuid(str(ticket_id))
    company_id_of(user)
    t = _norm(one(f'{TICKET_SELECT} WHERE t.id = %s', [tid]))
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
    return bool(t.get('resolved_at')) and (_now() - t['resolved_at']).total_seconds() * 1000 <= policy['reopenDays'] * 86_400_000


# ---------------------------------------------------------------- settings and categories

def get_settings(user: AuthUser) -> dict:
    return {'policy': helpdesk_policy(_company(user))}


def _require_hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


def put_settings(user: AuthUser, body: Any) -> dict:
    _require_hr(user)
    company_id = _company(user)
    policy = parse(POLICY_SCHEMA, body)
    with transaction.atomic():
        execute("""INSERT INTO company_setting (company_id, key, value, updated_by) VALUES (%s, 'helpdesk.policy', %s, %s)
                   ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
                [company_id, json.dumps(policy), user.id])
        audit('update', 'company_setting', actor_id=user.id, row_id='helpdesk.policy', after=policy)
    return {'policy': policy}


def list_categories(user: AuthUser) -> list[dict]:
    company_id = _company(user)
    hr = has_role(user, 'hr')
    owner = 'c.owner_user_id, u.name AS owner_name' if hr else 'NULL::uuid AS owner_user_id, NULL::text AS owner_name'
    return [_norm(r) for r in rows(
        f"""SELECT c.id, c.name, c.description, c.confidential, c.sort, c.active, {owner}
              FROM helpdesk_category c LEFT JOIN app_user u ON u.id = c.owner_user_id
             WHERE c.company_id = %s AND (c.active OR %s) ORDER BY c.sort, c.name""", [company_id, hr])]


def _check_owner(company_id: str, owner_user_id: str | None, confidential: bool) -> None:
    if not owner_user_id:
        return
    u = one("SELECT roles FROM app_user WHERE id = %s AND company_id = %s AND status = 'active'", [owner_user_id, company_id])
    if not u:
        raise BadRequestError('the owner must be an active user in your company')
    if confidential and not any(x in ('hr', 'admin') for x in u['roles']):
        raise BadRequestError('a confidential category can only be owned by HR')


def create_category(user: AuthUser, body: Any) -> dict:
    _require_hr(user)
    company_id = _company(user)
    b = parse(CATEGORY_BODY, body)
    _check_owner(company_id, b.get('ownerUserId'), b['confidential'])
    try:
        with transaction.atomic():
            r = one("""INSERT INTO helpdesk_category (company_id, name, description, confidential, owner_user_id, sort, active)
                       VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    [company_id, b['name'], b.get('description'), b['confidential'], b.get('ownerUserId'), b['sort'], b['active']])
    except IntegrityError as e:
        if getattr(getattr(e, '__cause__', None), 'sqlstate', None) == '23505':
            raise ConflictError('a category with this name already exists') from e
        raise
    cid = str(r['id'])
    audit('create', 'helpdesk_category', actor_id=user.id, row_id=cid, after=b)
    return {'id': cid}


def update_category(user: AuthUser, category_id: Any, body: Any) -> dict:
    _require_hr(user)
    company_id = _company(user)
    cid = parse_uuid(str(category_id))
    b = parse(CATEGORY_BODY, body)
    _check_owner(company_id, b.get('ownerUserId'), b['confidential'])
    try:
        with transaction.atomic():
            u = rows("""UPDATE helpdesk_category SET name = %s, description = %s, confidential = %s, owner_user_id = %s, sort = %s, active = %s
                         WHERE id = %s AND company_id = %s RETURNING id""",
                     [b['name'], b.get('description'), b['confidential'], b.get('ownerUserId'), b['sort'], b['active'], cid, company_id])
    except IntegrityError as e:
        if getattr(getattr(e, '__cause__', None), 'sqlstate', None) == '23505':
            raise ConflictError('a category with this name already exists') from e
        raise
    if not u:
        raise NotFoundError('category not found')
    audit('update', 'helpdesk_category', actor_id=user.id, row_id=cid, after=b)
    return {'ok': True}


def get_category(user: AuthUser, category_id: Any) -> dict:
    """One topic for HR's edit form."""
    _require_hr(user)
    company_id = _company(user)
    r = _norm(one("""SELECT c.id, c.name, c.description, c.confidential, c.sort, c.active, c.owner_user_id, u.name AS owner_name
                       FROM helpdesk_category c LEFT JOIN app_user u ON u.id = c.owner_user_id
                      WHERE c.id = %s AND c.company_id = %s""", [parse_uuid(str(category_id)), company_id]))
    if not r:
        raise NotFoundError('category not found')
    return r


def list_agents(user: AuthUser) -> list[dict]:
    """People HR can assign tickets to (HR for confidential categories)."""
    _require_hr(user)
    return [_norm(r) for r in rows(
        """SELECT id, name, email, roles, ('hr' = ANY(roles) OR 'admin' = ANY(roles)) AS is_hr
             FROM app_user WHERE company_id = %s AND status = 'active' ORDER BY name""", [_company(user)])]


# ---------------------------------------------------------------- tickets

def create_ticket(user: AuthUser, body: Any) -> dict:
    company_id = _company(user)
    b = parse(TICKET_BODY, body)
    c0 = _norm(one('SELECT id, confidential, owner_user_id, name FROM helpdesk_category WHERE id = %s AND company_id = %s AND active',
                   [b['categoryId'], company_id]))
    if not c0:
        raise BadRequestError('choose a category')
    due = _sla_due(company_id, b['priority'], _now())
    assignee = None if c0['confidential'] else c0['owner_user_id']
    with transaction.atomic():
        # Ticket numbers are sequential per company; the advisory lock keeps two submissions from taking the same one.
        execute("SELECT pg_advisory_xact_lock(hashtext('helpdesk:' || %s::text))", [company_id])
        n = one('SELECT coalesce(max(number), 0) + 1 AS n FROM helpdesk_ticket WHERE company_id = %s', [company_id])['n']
        t = one("""INSERT INTO helpdesk_ticket (company_id, number, requester_user_id, employee_id, category_id, subject, priority, sla_due_at, assignee_user_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, number""",
                [company_id, n, user.id, user.employee_id, b['categoryId'], b['subject'], b['priority'], due, assignee])
        tid = str(t['id'])
        msg = one("""INSERT INTO helpdesk_message (ticket_id, company_id, author_user_id, body, file_ids)
                     VALUES (%s, %s, %s, %s, %s::uuid[]) RETURNING id""", [tid, company_id, user.id, b['body'], b['fileIds']])
        _attach(company_id, user.id, str(msg['id']), b['fileIds'])
        targets = handlers({'company_id': company_id, 'assignee_user_id': assignee, 'owner_user_id': c0['owner_user_id'],
                            'confidential': c0['confidential']})
        for uid in targets:
            if uid == user.id:
                continue
            notify(company_id=company_id, user_id=uid, template='helpdesk.ticket.new', title=f"New ticket #{t['number']}: {c0['name']}",
                   body='A confidential ticket was raised. Open the help desk to read it.' if c0['confidential'] else b['subject'],
                   payload={'ticketId': tid}, channels=('push',))
    return {'id': tid, 'number': t['number'], 'slaDueAt': due}


def my_tickets(user: AuthUser) -> list[dict]:
    company_id = _company(user)
    out = []
    for r in rows(f"""{TICKET_SELECT} WHERE t.company_id = %s AND t.requester_user_id = %s
                      ORDER BY t.status IN ('resolved', 'closed'), t.updated_at DESC LIMIT 200""", [company_id, user.id]):
        r = _norm(r)
        r.pop('owner_user_id', None)
        out.append(r)
    return out


def queue(user: AuthUser, query: Any = None) -> list[dict]:
    """HR sees everything; others see the non-confidential tickets assigned to them or in categories they own."""
    company_id = _company(user)
    q = parse(QUEUE_QUERY, dict(query or {}))
    hr = has_role(user, 'hr')
    assignee = user.id if q.get('assignee') == 'me' else q.get('assignee')
    search = q['search'].strip() if q.get('search') is not None else None
    num = int(search.replace('#', '')) if search and re.fullmatch(r'#?\d+', search) else None
    term = re.sub(r'([\\%_])', r'\\\1', search) if search and num is None else None
    # The TS query let a number search match every ticket (its subject/name test was skipped as "no search"); here
    # "#12" or "12" finds ticket 12, as the search box promises.
    return [_norm(r) for r in rows(
        f"""{TICKET_SELECT}
             WHERE t.company_id = %(company)s
               AND (%(hr)s OR (NOT c.confidential AND (t.assignee_user_id = %(me)s::uuid OR c.owner_user_id = %(me)s::uuid)))
               AND (%(status)s::text IS NULL OR t.status = %(status)s::text OR (%(status)s::text = 'active' AND t.status IN ('open', 'in_progress', 'waiting')))
               AND (%(category)s::uuid IS NULL OR t.category_id = %(category)s::uuid) AND (%(priority)s::text IS NULL OR t.priority = %(priority)s::text)
               AND (%(assignee)s::text IS NULL OR (%(assignee)s::text = 'none' AND t.assignee_user_id IS NULL) OR t.assignee_user_id::text = %(assignee)s::text)
               AND (NOT %(breached)s OR (t.status IN ('open', 'in_progress', 'waiting') AND t.sla_due_at < now()))
               AND ((%(term)s::text IS NULL AND %(num)s::int IS NULL) OR t.subject ILIKE '%%' || %(term)s::text || '%%' OR ru.name ILIKE '%%' || %(term)s::text || '%%' OR t.number = %(num)s::int)
             ORDER BY t.status IN ('resolved', 'closed'), (t.status IN ('open', 'in_progress', 'waiting') AND t.sla_due_at < now()) DESC,
                      CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, t.sla_due_at LIMIT 500""",
        {'company': company_id, 'hr': hr, 'me': user.id, 'status': q.get('status'), 'category': q.get('categoryId'),
         'priority': q.get('priority'), 'assignee': assignee, 'breached': bool(q.get('breached')), 'term': term, 'num': num})]


def stats(user: AuthUser) -> dict:
    _require_hr(user)
    company_id = _company(user)
    s = one("""SELECT count(*) FILTER (WHERE status IN ('open', 'in_progress', 'waiting'))::int AS open,
              count(*) FILTER (WHERE status = 'open' AND assignee_user_id IS NULL)::int AS unassigned,
              count(*) FILTER (WHERE status IN ('open', 'in_progress', 'waiting') AND sla_due_at < now())::int AS overdue,
              count(*) FILTER (WHERE breached_at IS NOT NULL AND created_at > now() - interval '90 days')::int AS breached_90d,
              count(*) FILTER (WHERE resolved_at > now() - interval '30 days')::int AS resolved_30d,
              round(avg(extract(epoch FROM resolved_at - created_at) / 3600) FILTER (WHERE resolved_at > now() - interval '90 days')::numeric, 1)::text AS avg_resolution_hours,
              round(avg(extract(epoch FROM first_response_at - created_at) / 3600) FILTER (WHERE first_response_at > now() - interval '90 days')::numeric, 1)::text AS avg_first_response_hours,
              round(avg(rating) FILTER (WHERE rating IS NOT NULL AND created_at > now() - interval '180 days')::numeric, 2)::text AS avg_rating,
              count(*) FILTER (WHERE rating IS NOT NULL AND created_at > now() - interval '180 days')::int AS ratings
         FROM helpdesk_ticket WHERE company_id = %s""", [company_id])
    by_cat = rows("""SELECT c.name, count(t.*) FILTER (WHERE t.status IN ('open', 'in_progress', 'waiting'))::int AS open, count(t.*)::int AS total
                       FROM helpdesk_category c LEFT JOIN helpdesk_ticket t ON t.category_id = c.id WHERE c.company_id = %s
                      GROUP BY c.id ORDER BY c.sort""", [company_id])
    return {'stats': s, 'byCategory': by_cat}


def ticket_detail(user: AuthUser, ticket_id: Any) -> dict:
    _company(user)
    t, role = _load_ticket(user, ticket_id)
    msgs = [_norm(m) for m in rows(
        """SELECT m.id, m.body, m.internal, m.file_ids, m.created_at, m.author_user_id, u.name AS author_name, (m.author_user_id = %s) AS mine
             FROM helpdesk_message m JOIN app_user u ON u.id = m.author_user_id
            WHERE m.ticket_id = %s AND (NOT m.internal OR %s) ORDER BY m.created_at""", [user.id, t['id'], role == 'agent'])]
    ids = list(dict.fromkeys(f for m in msgs for f in m['file_ids']))
    files = [_norm(f) for f in rows("""SELECT id, filename, mime, bytes FROM stored_file
                                        WHERE id = ANY(%s::uuid[]) AND company_id = %s AND deleted_at IS NULL""",
                                     [ids, t['company_id']])] if ids else []
    policy = helpdesk_policy(t['company_id'])
    can_reopen = role == 'requester' and t['status'] == 'resolved' and _within_reopen(t, policy)
    ticket = dict(t)
    if role != 'agent':
        ticket.pop('owner_user_id', None)
    ticket['overdue'] = t['status'] in ACTIVE and t['sla_due_at'] < _now()
    return {'ticket': ticket, 'role': role, 'messages': msgs, 'files': files, 'canReopen': can_reopen}


def add_message(user: AuthUser, ticket_id: Any, body: Any) -> dict:
    _company(user)
    t, role = _load_ticket(user, ticket_id)
    b = parse(MESSAGE_BODY, body)
    if b['internal'] and role != 'agent':
        raise ForbiddenError('only the help desk team can add internal notes')
    if t['status'] == 'closed':
        raise ConflictError('this ticket is closed; raise a new ticket instead')
    policy = helpdesk_policy(t['company_id'])
    if t['status'] == 'resolved' and role == 'requester' and not _within_reopen(t, policy):
        raise ConflictError('this ticket was resolved too long ago; raise a new ticket instead')
    with transaction.atomic():
        ins = one("""INSERT INTO helpdesk_message (ticket_id, company_id, author_user_id, body, internal, file_ids)
                     VALUES (%s, %s, %s, %s, %s, %s::uuid[]) RETURNING id""",
                  [t['id'], t['company_id'], user.id, b['body'], b['internal'], b['fileIds']])
        mid = str(ins['id'])
        _attach(t['company_id'], user.id, mid, b['fileIds'])
        if role == 'agent' and not b['internal']:
            # A public answer from the team counts as the first response and moves a new ticket along.
            execute("""UPDATE helpdesk_ticket SET first_response_at = coalesce(first_response_at, now()),
                         status = CASE WHEN status = 'open' THEN 'in_progress' ELSE status END, updated_at = now() WHERE id = %s""", [t['id']])
            if t['requester_user_id'] != user.id:
                notify(company_id=t['company_id'], user_id=t['requester_user_id'], template='helpdesk.ticket.reply',
                       title=f"Reply on ticket #{t['number']}", body=_js_slice(b['body'], 200), payload={'ticketId': t['id']},
                       channels=('push',))
        elif role == 'requester':
            # The requester answering (or writing again after a resolution) puts the ticket back in the team's hands.
            execute("""UPDATE helpdesk_ticket SET status = CASE WHEN status IN ('waiting', 'resolved') THEN 'in_progress' ELSE status END,
                         resolved_at = CASE WHEN status = 'resolved' THEN NULL ELSE resolved_at END, updated_at = now() WHERE id = %s""", [t['id']])
            for uid in handlers(t):
                if uid != user.id:
                    notify(company_id=t['company_id'], user_id=uid, template='helpdesk.ticket.reply',
                           title=f"New message on ticket #{t['number']}",
                           body='Open the help desk to read it.' if t['confidential'] else _js_slice(b['body'], 200),
                           payload={'ticketId': t['id']})
        else:
            execute('UPDATE helpdesk_ticket SET updated_at = now() WHERE id = %s', [t['id']])
    return {'id': mid}


def _js_slice(s: str, n: int) -> str:
    """String.prototype.slice counts UTF-16 code units."""
    enc = s.encode('utf-16-le')
    if len(enc) <= n * 2:
        return s
    return enc[:n * 2].decode('utf-16-le', errors='ignore')


def update_ticket(user: AuthUser, ticket_id: Any, body: Any) -> dict:
    _company(user)
    t, role = _load_ticket(user, ticket_id)
    if role != 'agent':
        raise ForbiddenError('only the help desk team can change a ticket')
    b = parse(PATCH_BODY, body)
    hr = has_role(user, 'hr')
    if ('assigneeUserId' in b or 'categoryId' in b) and not hr:
        raise ForbiddenError('only HR can reassign or move a ticket')
    confidential = t['confidential']
    if b.get('categoryId'):
        c = one('SELECT confidential FROM helpdesk_category WHERE id = %s AND company_id = %s', [b['categoryId'], t['company_id']])
        if not c:
            raise BadRequestError('category not found')
        confidential = c['confidential']
    new_assignee = b['assigneeUserId'] if 'assigneeUserId' in b else t['assignee_user_id']
    if new_assignee:
        u = one("SELECT roles FROM app_user WHERE id = %s AND company_id = %s AND status = 'active'", [new_assignee, t['company_id']])
        if not u:
            raise BadRequestError('the assignee must be an active user in your company')
        if confidential and not any(x in ('hr', 'admin') for x in u['roles']):
            raise BadRequestError('confidential tickets can only be assigned to HR')
        if new_assignee == t['requester_user_id']:
            raise BadRequestError('a ticket cannot be assigned to the person who raised it')
    priority = b.get('priority') or t['priority']
    due = _sla_due(t['company_id'], priority, t['created_at']) if b.get('priority') and b['priority'] != t['priority'] else None
    status = b.get('status')
    with transaction.atomic():
        execute("""UPDATE helpdesk_ticket SET status = coalesce(%(status)s::text, status), priority = %(priority)s, assignee_user_id = %(assignee)s::uuid,
                     category_id = coalesce(%(category)s::uuid, category_id),
                     sla_due_at = coalesce(%(due)s::timestamptz, sla_due_at),
                     breached_at = CASE WHEN %(due)s::timestamptz IS NOT NULL AND %(due)s::timestamptz > now() THEN NULL ELSE breached_at END,
                     resolved_at = CASE WHEN %(status)s::text = 'resolved' AND status <> 'resolved' THEN now()
                                        WHEN %(status)s::text IN ('open', 'in_progress', 'waiting') THEN NULL ELSE resolved_at END,
                     closed_at = CASE WHEN %(status)s::text = 'closed' THEN now() WHEN %(status)s::text IS NOT NULL THEN NULL ELSE closed_at END,
                     updated_at = now() WHERE id = %(id)s""",
                {'id': t['id'], 'status': status, 'priority': priority, 'assignee': new_assignee, 'category': b.get('categoryId'), 'due': due})
        audit('update', 'helpdesk_ticket', actor_id=user.id, row_id=t['id'],
              before={'status': t['status'], 'priority': t['priority'], 'assignee': t['assignee_user_id']}, after=b)
        if b.get('assigneeUserId') and b['assigneeUserId'] != t['assignee_user_id'] and b['assigneeUserId'] != user.id:
            notify(company_id=t['company_id'], user_id=b['assigneeUserId'], template='helpdesk.ticket.assigned',
                   title=f"Ticket #{t['number']} assigned to you",
                   body='A confidential ticket. Open the help desk to read it.' if confidential else t['subject'],
                   payload={'ticketId': t['id']}, channels=('push',))
        if status == 'resolved' and t['status'] != 'resolved' and t['requester_user_id'] != user.id:
            notify(company_id=t['company_id'], user_id=t['requester_user_id'], template='helpdesk.ticket.resolved',
                   title=f"Ticket #{t['number']} resolved", body='Tell us how we did, or reply if you still need help.',
                   payload={'ticketId': t['id']}, channels=('push',))
    return {'ok': True}


def reopen_ticket(user: AuthUser, ticket_id: Any) -> dict:
    _company(user)
    t, role = _load_ticket(user, ticket_id)
    if role != 'requester' and not has_role(user, 'hr'):
        raise ForbiddenError('only the person who raised the ticket can reopen it')
    if t['status'] != 'resolved':
        raise ConflictError('this ticket is closed; raise a new ticket instead' if t['status'] == 'closed' else 'this ticket is still open')
    policy = helpdesk_policy(t['company_id'])
    if role == 'requester' and not _within_reopen(t, policy):
        raise ConflictError(f"tickets can be reopened within {policy['reopenDays']} days of being resolved")
    with transaction.atomic():
        execute("""UPDATE helpdesk_ticket SET status = 'in_progress', resolved_at = NULL, rating = NULL, rating_comment = NULL,
                     updated_at = now() WHERE id = %s""", [t['id']])
        for uid in handlers(t):
            if uid != user.id:
                notify(company_id=t['company_id'], user_id=uid, template='helpdesk.ticket.reopened', title=f"Ticket #{t['number']} reopened",
                       body='Open the help desk to read it.' if t['confidential'] else t['subject'], payload={'ticketId': t['id']})
        audit('reopen', 'helpdesk_ticket', actor_id=user.id, row_id=t['id'])
    return {'ok': True}


def close_ticket(user: AuthUser, ticket_id: Any) -> dict:
    _company(user)
    t, role = _load_ticket(user, ticket_id)
    if role == 'requester' and t['status'] not in ('resolved', 'open', 'in_progress', 'waiting'):
        raise ConflictError('this ticket is already closed')
    if t['status'] == 'closed':
        raise ConflictError('this ticket is already closed')
    with transaction.atomic():
        execute("""UPDATE helpdesk_ticket SET status = 'closed', closed_at = now(), resolved_at = coalesce(resolved_at, now()),
                     updated_at = now() WHERE id = %s""", [t['id']])
        audit('close', 'helpdesk_ticket', actor_id=user.id, row_id=t['id'])
    return {'ok': True}


def rate_ticket(user: AuthUser, ticket_id: Any, body: Any) -> dict:
    _company(user)
    t, _role = _load_ticket(user, ticket_id)
    if t['requester_user_id'] != user.id:
        raise ForbiddenError('only the person who raised the ticket can rate it')
    b = parse(RATE_BODY, body)
    if t['status'] not in ('resolved', 'closed'):
        raise ConflictError('you can rate a ticket once it is resolved')
    if t['rating'] is not None:
        raise ConflictError('you have already rated this ticket')
    with transaction.atomic():
        execute('UPDATE helpdesk_ticket SET rating = %s, rating_comment = %s, updated_at = now() WHERE id = %s',
                [b['rating'], b.get('comment'), t['id']])
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
    fid = parse_uuid(str(file_id))
    m = one('SELECT 1 AS x FROM helpdesk_message WHERE ticket_id = %s AND %s::uuid = ANY(file_ids) AND (NOT internal OR %s) LIMIT 1',
            [t['id'], fid, role == 'agent'])
    if not m:
        raise NotFoundError('file not found')
    return read_file(t['company_id'], fid)


# ---------------------------------------------------------------- daily job

def helpdesk_daily(now: datetime | None = None) -> dict:
    """Close tickets resolved long enough ago, and escalate tickets that passed their SLA without being resolved."""
    at = now or _now()
    with transaction.atomic():
        closed = execute(
            """UPDATE helpdesk_ticket t SET status = 'closed', closed_at = %(at)s, updated_at = %(at)s
                 FROM company c LEFT JOIN company_setting s ON s.company_id = c.id AND s.key = 'helpdesk.policy'
                WHERE t.company_id = c.id AND t.status = 'resolved'
                  AND t.resolved_at < %(at)s::timestamptz - make_interval(days => coalesce((s.value->>'autoCloseDays')::int, 7))""",
            {'at': at})
        breached = [_norm(r) for r in rows(
            """UPDATE helpdesk_ticket t SET breached_at = %(at)s FROM helpdesk_category c
                WHERE c.id = t.category_id AND t.status IN ('open', 'in_progress', 'waiting') AND t.breached_at IS NULL AND t.sla_due_at < %(at)s
                RETURNING t.*, c.confidential, c.owner_user_id, c.name AS category_name""", {'at': at})]
        for t in breached:
            targets = list(dict.fromkeys([*handlers(t), *user_ids_with_role(t['company_id'], 'hr')]))
            for uid in targets:
                notify(company_id=t['company_id'], user_id=uid, template='helpdesk.sla_breached',
                       title=f"Ticket #{t['number']} is past its due time",
                       body=f"{'A confidential ticket' if t['confidential'] else chr(34) + t['subject'] + chr(34)} ({t['priority']} priority) has not been resolved in time.",
                       payload={'ticketId': t['id']}, channels=('push',))
    return {'ticketsClosed': closed, 'ticketsBreached': len(breached)}
