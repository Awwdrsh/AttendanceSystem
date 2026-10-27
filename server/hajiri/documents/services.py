"""
Documents & letters (port of apps/api/src/modules/documents/routes.ts): employee documents with verification,
versions and expiry alerts; letter templates, issued letters with reference numbers and letter requests (through the
workflow engine); company policies with acknowledgements and reminders.

Every public function takes the signed-in AuthUser and, where the TS route had one, a body or query in the TS API's
shape (camelCase keys), validated with the same rules and messages as the zod schemas. Errors are the TS ones.
"""
from __future__ import annotations

import json
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from django.db import transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import now as clock_now
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import Arr, Bool, Enum, Int, IsoDate, Obj, Str, Uuid, parse
from hajiri.files.store import attach_file, delete_file, read_file
from hajiri.notifications.service import notify, notify_role, user_id_for_employee
from hajiri.workflow.service import DecisionContext, act, cancel_workflow, register_handler, start_workflow

from .letters import (PLACEHOLDERS, company_head, employee_letter_values, esc, letter_settings, next_ref_no, npt_today,
                      render_body, render_letter_html, render_subject, seed_document_defaults, subject_text,
                      unknown_placeholders)

# ---------------------------------------------------------------- shared helpers

_seeded: set[str] = set()


def ensure_defaults(company_id: str) -> None:
    """Companies created before this module get their categories and built-in templates on first use."""
    if company_id in _seeded:
        return
    seed_document_defaults(company_id)
    _seeded.add(company_id)


def _norm(v: Any) -> Any:
    if isinstance(v, _uuid.UUID):
        return str(v)
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def _r(r: dict | None) -> dict | None:
    return None if r is None else {k: _norm(v) for k, v in r.items()}


def _rs(rs: list[dict]) -> list[dict]:
    return [_r(r) for r in rs]


def is_hr(u: AuthUser) -> bool:
    return has_role(u, 'hr')


def _require_hr(u: AuthUser) -> None:
    if not is_hr(u):
        raise ForbiddenError('requires role hr')


def _assert_employee_in_company(company_id: str, employee_id: str) -> dict:
    r = one('SELECT name_en, emp_code, status FROM employee WHERE id = %s AND company_id = %s', [employee_id, company_id])
    if not r:
        raise NotFoundError('employee not found')
    return r


def _uuid_param(v: Any) -> str:
    return parse(Uuid(), str(v))


# ---------------------------------------------------------------- policy audience

def audience_ids(company_id: str, p: dict, only_employee: str | None = None) -> list[str]:
    """Active employees a policy is addressed to: everyone, chosen branches, or chosen departments (including sub-units)."""
    r = rows("""
        WITH RECURSIVE anc(unit_id, ancestor_id, depth) AS (
            SELECT id, id, 0 FROM org_unit WHERE company_id = %(c)s
            UNION ALL
            SELECT anc.unit_id, o.parent_id, anc.depth + 1 FROM anc JOIN org_unit o ON o.id = anc.ancestor_id
             WHERE o.parent_id IS NOT NULL AND anc.depth < 20)
        SELECT e.id FROM employee e
          LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> %(d)s::date
         WHERE e.company_id = %(c)s AND e.status = 'active' AND (%(only)s::uuid IS NULL OR e.id = %(only)s::uuid)
           AND CASE %(aud)s::text
                 WHEN 'all' THEN true
                 WHEN 'branch' THEN coalesce(a.branch_id = ANY(%(ids)s::uuid[]), false)
                 ELSE EXISTS (SELECT 1 FROM anc WHERE anc.unit_id = a.org_unit_id AND anc.ancestor_id = ANY(%(ids)s::uuid[]))
               END""",
        {'c': company_id, 'aud': p['audience'], 'ids': [str(x) for x in (p.get('audience_ids') or [])],
         'only': only_employee, 'd': npt_today()})
    return [str(x['id']) for x in r]


# ---------------------------------------------------------------- letter requests (workflow)

def apply_letter_request_decision(request_id: str, state: str, ctx: DecisionContext) -> None:
    lr = one("""SELECT lr.employee_id, lr.company_id, lr.status, t.name FROM letter_request lr
                  JOIN letter_template t ON t.id = lr.template_id WHERE lr.id = %s FOR UPDATE OF lr""", [request_id])
    if not lr:
        raise NotFoundError('letter request not found')
    if lr['status'] != 'pending':
        raise ConflictError(f"request already {lr['status']}")
    execute('UPDATE letter_request SET status = %s, decision_reason = %s, decided_at = now() WHERE id = %s',
            [state, ctx.reason, request_id])
    if state == 'rejected':
        emp = str(lr['employee_id'])
        notify(company_id=str(lr['company_id']), user_id=user_id_for_employee(emp), employee_id=emp,
               template='letter_request.rejected', title=f"{lr['name']} request declined",
               body=f"HR declined your request for a {lr['name'].lower()}{': ' + ctx.reason if ctx.reason else '.'}",
               payload={'requestId': str(request_id)}, channels=('push',))


register_handler('letter_request', apply_letter_request_decision)


# ---------------------------------------------------------------- daily job

def documents_daily_job(now: datetime | None = None) -> dict:
    """
    Expiry alerts at 30, 7 and 0 days before a document expires (one per document and threshold; HR gets one summary
    per company), and a weekly reminder to employees who have not acknowledged a policy that needs acknowledgement.
    """
    now = now or clock_now()
    today = (now.astimezone(timezone.utc) + timedelta(minutes=345)).date().isoformat()
    docs = rows("""
        SELECT d.id, d.company_id, d.employee_id, d.title, d.expiry_date::text AS expiry_date, (d.expiry_date - %s::date) AS days,
               d.visible_to_employee, e.name_en
          FROM employee_document d JOIN employee e ON e.id = d.employee_id
         WHERE d.deleted_at IS NULL AND d.is_current AND d.status <> 'rejected' AND d.expiry_date IS NOT NULL AND e.status = 'active'
           AND d.expiry_date - %s::date BETWEEN -30 AND 30
         ORDER BY d.expiry_date""", [today, today])
    expiry_alerts = 0
    per_company: dict[str, list[str]] = {}
    for d in docs:
        days = d['days']
        threshold = 0 if days <= 0 else 7 if days <= 7 else 30
        with transaction.atomic():
            if not execute('INSERT INTO document_expiry_alert (document_id, threshold) VALUES (%s, %s) ON CONFLICT DO NOTHING',
                           [d['id'], threshold]):
                continue
            expiry_alerts += 1
            when = (f"expired on {bs_label(d['expiry_date'])}" if days < 0 else 'expires today' if days == 0
                    else f"expires on {bs_label(d['expiry_date'])} (in {days} days)")
            if d['visible_to_employee']:
                user_id = user_id_for_employee(str(d['employee_id']))
                if user_id:
                    notify(company_id=str(d['company_id']), user_id=user_id, employee_id=str(d['employee_id']),
                           template='document.expiring',
                           title=f"{d['title']} {'has expired' if days < 0 else 'is expiring'}",
                           body=f"Your {d['title']} {when}. Upload the renewed document under Profile, Documents & letters.",
                           payload={'documentId': str(d['id']), 'threshold': threshold}, channels=('push',))
        per_company.setdefault(str(d['company_id']), []).append(f"{d['name_en']}: {d['title']} {when}")
    for company_id, lines in per_company.items():
        notify_role(company_id, 'hr', template='document.expiring.summary',
                    title=f"{len(lines)} employee document{'' if len(lines) == 1 else 's'} expiring",
                    body='; '.join(lines[:10]) + (f'; and {len(lines) - 10} more' if len(lines) > 10 else ''),
                    payload={'count': len(lines)})

    policy_nudges = 0
    policies = rows("""SELECT id, company_id, title, audience, audience_ids FROM company_policy
                        WHERE archived_at IS NULL AND requires_ack AND published_at < %s::timestamptz - interval '2 days'""", [now])
    for p in policies:
        cid = str(p['company_id'])
        ids = audience_ids(cid, p)
        if not ids:
            continue
        due = rows("""
            SELECT x AS employee_id FROM unnest(%s::uuid[]) x
             WHERE NOT EXISTS (SELECT 1 FROM policy_ack a WHERE a.policy_id = %s AND a.employee_id = x)
               AND NOT EXISTS (SELECT 1 FROM policy_nudge n WHERE n.policy_id = %s AND n.employee_id = x
                                  AND n.last_sent_at > %s::timestamptz - interval '7 days')""", [ids, p['id'], p['id'], now])
        for r in due:
            employee_id = str(r['employee_id'])
            user_id = user_id_for_employee(employee_id)
            if not user_id:
                continue
            with transaction.atomic():
                notify(company_id=cid, user_id=user_id, employee_id=employee_id, template='policy.reminder',
                       title=f"Please read: {p['title']}",
                       body=f"{p['title']} is waiting for your acknowledgement under Profile, Documents & letters.",
                       payload={'policyId': str(p['id'])})
                execute("""INSERT INTO policy_nudge (policy_id, employee_id, last_sent_at) VALUES (%s, %s, %s)
                           ON CONFLICT (policy_id, employee_id) DO UPDATE SET last_sent_at = EXCLUDED.last_sent_at""",
                        [p['id'], employee_id, now])
            policy_nudges += 1
    return {'expiryAlerts': expiry_alerts, 'policyNudges': policy_nudges}


# ---------------------------------------------------------------- schemas

_CODE = dict(regex=r'^[a-z0-9_]{2,40}$', regex_message='lowercase letters, digits and _')

CATEGORY_BODY = Obj({
    'code': Str(**_CODE),
    'name': Str(trim=True, min=1, max=80),
    'employeeCanUpload': Bool(default=True),
    'expiryRequired': Bool(default=False),
    'visibleToEmployee': Bool(default=True),
    'sortOrder': Int(min=0, max=999, default=50),
})
CATEGORY_PATCH = Obj({
    'name': Str(trim=True, min=1, max=80, optional=True),
    'employeeCanUpload': Bool(optional=True),
    'expiryRequired': Bool(optional=True),
    'visibleToEmployee': Bool(optional=True),
    'sortOrder': Int(min=0, max=999, optional=True),
    'active': Bool(optional=True),
})
DOCUMENT_BODY = Obj({
    'categoryId': Uuid(),
    'title': Str(trim=True, min=1, max=120),
    'fileId': Uuid(),
    'expiryDate': IsoDate(nullish=True),
    'visibleToEmployee': Bool(optional=True),
    'notes': Str(trim=True, max=500, nullish=True),
    'replacesId': Uuid(nullish=True),
})
DOCUMENT_PATCH = Obj({
    'title': Str(trim=True, min=1, max=120, optional=True),
    'expiryDate': IsoDate(nullish=True),
    'visibleToEmployee': Bool(optional=True),
    'notes': Str(trim=True, max=500, nullish=True),
})
VERIFY_BODY = Obj({'decision': Enum(('verify', 'reject')), 'reason': Str(trim=True, max=300, optional=True)})
TEMPLATE_BODY = Obj({
    'code': Str(**_CODE),
    'name': Str(trim=True, min=1, max=80),
    'subject': Str(trim=True, min=1, max=160),
    'body': Str(trim=True, min=1, max=20000),
    'requestable': Bool(default=False),
    'visibleToEmployee': Bool(default=True),
})
TEMPLATE_PATCH = Obj({
    'name': Str(trim=True, min=1, max=80, optional=True),
    'subject': Str(trim=True, min=1, max=160, optional=True),
    'body': Str(trim=True, min=1, max=20000, optional=True),
    'requestable': Bool(optional=True),
    'visibleToEmployee': Bool(optional=True),
    'active': Bool(optional=True),
})
SETTINGS_BODY = Obj({
    'signatoryName': Str(trim=True, max=80, nullable=True),
    'signatoryTitle': Str(trim=True, max=80, nullable=True),
    'refPrefix': Str(trim=True, regex=r'^[A-Za-z0-9/_-]{1,20}$', regex_message='letters, digits, / _ - only'),
})
PREVIEW_BODY = Obj({
    'employeeId': Uuid(), 'templateId': Uuid(optional=True), 'subject': Str(max=160, optional=True),
    'body': Str(max=20000, optional=True), 'purpose': Str(max=300, nullish=True), 'addressedTo': Str(max=200, nullish=True),
})
ISSUE_BODY = Obj({
    'templateId': Uuid(), 'employeeId': Uuid(), 'visibleToEmployee': Bool(optional=True),
    'purpose': Str(trim=True, max=300, nullish=True), 'addressedTo': Str(trim=True, max=200, nullish=True),
    'requestId': Uuid(optional=True), 'allowMissing': Bool(default=False),
})
LETTERS_QUERY = Obj({'employeeId': Uuid(optional=True), 'q': Str(max=80, optional=True),
                     'limit': Int(min=1, max=500, default=200, coerce=True)})
VOID_BODY = Obj({'reason': Str(trim=True, min=3, max=300)})
REQUEST_BODY = Obj({'templateId': Uuid(), 'purpose': Str(trim=True, min=3, max=300),
                    'addressedTo': Str(trim=True, max=200, nullish=True)})
REQUESTS_QUERY = Obj({'status': Enum(('pending', 'approved', 'rejected', 'issued', 'cancelled', 'open'), optional=True)})
POLICY_BODY = Obj({
    'title': Str(trim=True, min=1, max=160),
    'description': Str(trim=True, max=2000, nullish=True),
    'fileId': Uuid(),
    'audience': Enum(('all', 'branch', 'department'), default='all'),
    'audienceIds': Arr(Uuid(), max=100, default=list),
    'requiresAck': Bool(default=True),
})
POLICY_PATCH = Obj({
    'title': Str(trim=True, min=1, max=160, optional=True),
    'description': Str(trim=True, max=2000, nullish=True),
    'requiresAck': Bool(optional=True),
    'archived': Bool(optional=True),
})
HISTORY_QUERY = Obj({'history': Enum(('0', '1'), optional=True)})
QUEUE_QUERY = Obj({'days': Int(min=0, max=365, default=30, coerce=True)})
ARCHIVED_QUERY = Obj({'archived': Enum(('0', '1'), optional=True)})


def _q(schema: Obj, query: dict | None) -> dict:
    """A query string as zod saw it: empty values count as absent."""
    return parse(schema, {k: v for k, v in (query or {}).items() if v not in (None, '')})


# ---------------------------------------------------------------- categories

def list_categories(user: AuthUser) -> list[dict]:
    company_id = company_id_of(user)
    ensure_defaults(company_id)
    return _rs(rows("""SELECT id, code, name, employee_can_upload, expiry_required, visible_to_employee, sort_order, active
                         FROM document_category WHERE company_id = %s AND (%s OR active) ORDER BY sort_order, name""",
                    [company_id, is_hr(user)]))


def create_category(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = parse(CATEGORY_BODY, body)
    with transaction.atomic():
        r = one("""INSERT INTO document_category (company_id, code, name, employee_can_upload, expiry_required, visible_to_employee, sort_order)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [company_id, b['code'], b['name'], b['employeeCanUpload'], b['expiryRequired'], b['visibleToEmployee'],
                 b['sortOrder']])
        audit('create', 'document_category', actor_id=user.id, row_id=r['id'], after=b)
    return {'id': str(r['id'])}


def update_category(user: AuthUser, category_id: str, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    cid = _uuid_param(category_id)
    b = parse(CATEGORY_PATCH, body)
    with transaction.atomic():
        n = execute("""UPDATE document_category SET name = coalesce(%s, name), employee_can_upload = coalesce(%s, employee_can_upload),
                              expiry_required = coalesce(%s, expiry_required), visible_to_employee = coalesce(%s, visible_to_employee),
                              sort_order = coalesce(%s, sort_order), active = coalesce(%s, active)
                        WHERE id = %s AND company_id = %s""",
                    [b.get('name'), b.get('employeeCanUpload'), b.get('expiryRequired'), b.get('visibleToEmployee'),
                     b.get('sortOrder'), b.get('active'), cid, company_id])
        if not n:
            raise NotFoundError('category not found')
        audit('update', 'document_category', actor_id=user.id, row_id=cid, after=b)
    return {'ok': True}


# ---------------------------------------------------------------- employee documents

DOC_COLS = """d.id, d.company_id, d.employee_id, d.category_id, c.name AS category_name, d.title, d.file_id, d.version, d.replaces_id,
       d.is_current, d.status, d.reject_reason, d.expiry_date::text AS expiry_date, d.visible_to_employee, d.notes, d.uploaded_by,
       u.name AS uploaded_by_name, d.verified_at, d.created_at, f.filename, f.mime, f.bytes"""
DOC_FROM = """FROM employee_document d JOIN document_category c ON c.id = d.category_id JOIN stored_file f ON f.id = d.file_id
  LEFT JOIN app_user u ON u.id = d.uploaded_by"""


def _list_docs(user: AuthUser, employee_id: str, include_history: bool) -> list[dict]:
    """Who may see an employee's documents: HR all of them; the employee their own visible ones and their own uploads."""
    company_id = company_id_of(user)
    hr = is_hr(user)
    if not hr and user.employee_id != employee_id:
        raise ForbiddenError('you can only see your own documents')
    _assert_employee_in_company(company_id, employee_id)
    return _rs(rows(f"""SELECT {DOC_COLS} {DOC_FROM}
                         WHERE d.company_id = %s AND d.employee_id = %s AND d.deleted_at IS NULL AND (%s OR d.is_current)
                           AND (%s OR d.visible_to_employee OR d.uploaded_by = %s)
                         ORDER BY c.sort_order, c.name, d.title, d.version DESC""",
                    [company_id, employee_id, include_history and hr, hr, user.id]))


def my_documents(user: AuthUser) -> list[dict]:
    if not user.employee_id:
        return []
    ensure_defaults(company_id_of(user))
    return _list_docs(user, user.employee_id, False)


def employee_documents(user: AuthUser, employee_id: str, query: dict | None = None) -> list[dict]:
    eid = _uuid_param(employee_id)
    q = _q(HISTORY_QUERY, query)
    return _list_docs(user, eid, q.get('history') == '1')


def queue(user: AuthUser, query: dict | None = None) -> dict:
    """HR queues: documents awaiting verification and documents expiring soon."""
    _require_hr(user)
    company_id = company_id_of(user)
    q = _q(QUEUE_QUERY, query)
    pending = rows(f"""SELECT e.emp_code, e.name_en, {DOC_COLS} {DOC_FROM} JOIN employee e ON e.id = d.employee_id
                        WHERE d.company_id = %s AND d.deleted_at IS NULL AND d.status = 'pending' ORDER BY d.created_at""",
                   [company_id])
    expiring = rows(f"""SELECT e.emp_code, e.name_en, (d.expiry_date - %s::date) AS days_left, {DOC_COLS} {DOC_FROM}
                          JOIN employee e ON e.id = d.employee_id
                         WHERE d.company_id = %s AND d.deleted_at IS NULL AND d.is_current AND d.status <> 'rejected' AND e.status = 'active'
                           AND d.expiry_date IS NOT NULL AND d.expiry_date <= %s::date + %s::int ORDER BY d.expiry_date""",
                    [npt_today(), company_id, npt_today(), q['days']])
    return {'pending': _rs(pending), 'expiring': _rs(expiring)}


def add_document(user: AuthUser, employee_id: str, body: dict) -> dict:
    company_id = company_id_of(user)
    eid = _uuid_param(employee_id)
    b = parse(DOCUMENT_BODY, body)
    hr = is_hr(user)
    if not hr and user.employee_id != eid:
        raise ForbiddenError('you can only upload your own documents')
    emp = _assert_employee_in_company(company_id, eid)
    c = one("""SELECT name, employee_can_upload, expiry_required, visible_to_employee, active FROM document_category
                WHERE id = %s AND company_id = %s""", [b['categoryId'], company_id])
    if not c or not c['active']:
        raise BadRequestError('choose a document category')
    if not hr and not c['employee_can_upload']:
        raise ForbiddenError(f"only HR can add a {c['name'].lower()}")
    expiry = b.get('expiryDate')
    if c['expiry_required'] and not expiry:
        raise BadRequestError(f"{c['name']} needs an expiry date")
    replaces = b.get('replacesId')
    visible = b['visibleToEmployee'] if b.get('visibleToEmployee') is not None else c['visible_to_employee']
    with transaction.atomic():
        version = 1
        if replaces:
            o = one("""SELECT employee_id, version, is_current, uploaded_by, status, visible_to_employee FROM employee_document
                        WHERE id = %s AND company_id = %s AND deleted_at IS NULL FOR UPDATE""", [replaces, company_id])
            if (not o or str(o['employee_id']) != eid
                    or (not hr and not o['visible_to_employee'] and str(o['uploaded_by'] or '') != user.id)):
                raise NotFoundError('the document to replace was not found')
            if not o['is_current']:
                raise ConflictError('only the latest version can be replaced')
            version = o['version'] + 1
            execute('UPDATE employee_document SET is_current = false WHERE id = %s', [replaces])
        r = one("""INSERT INTO employee_document (company_id, employee_id, category_id, title, file_id, version, replaces_id, status,
                                                  expiry_date, visible_to_employee, notes, uploaded_by, verified_by, verified_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [company_id, eid, b['categoryId'], b['title'], b['fileId'], version, replaces, 'verified' if hr else 'pending',
                 expiry, visible if hr else True, b.get('notes'), user.id, user.id if hr else None,
                 clock_now() if hr else None])
        doc_id = str(r['id'])
        attach_file(company_id=company_id, file_id=b['fileId'], user_id=user.id, purpose='employee_document',
                    attached_to=f'employee_document:{doc_id}')
        audit('create', 'employee_document', actor_id=user.id, row_id=doc_id,
              after={'employeeId': eid, 'categoryId': b['categoryId'], 'title': b['title'], 'version': version,
                     'expiryDate': expiry})
        if not hr:
            notify_role(company_id, 'hr', template='document.uploaded', title='Document to verify',
                        body=f"{emp['name_en']} ({emp['emp_code']}) uploaded {b['title']}.",
                        payload={'documentId': doc_id, 'employeeId': eid})
        elif user.employee_id != eid and visible:
            notify(company_id=company_id, user_id=user_id_for_employee(eid), employee_id=eid, template='document.added',
                   title='New document in your profile', body=f"HR added {b['title']} to your documents.",
                   payload={'documentId': doc_id})
    return {'id': doc_id}


def load_doc(company_id: str, doc_id: str) -> dict:
    d = one(f'SELECT {DOC_COLS} {DOC_FROM} WHERE d.id = %s AND d.company_id = %s AND d.deleted_at IS NULL', [doc_id, company_id])
    if not d:
        raise NotFoundError('document not found')
    return _r(d)


def document_for(user: AuthUser, doc_id: str) -> dict:
    """A document the user may see (HR: any in the company; the employee: their own visible ones and own uploads)."""
    d = load_doc(company_id_of(user), _uuid_param(doc_id))
    own = user.employee_id == d['employee_id'] and (d['visible_to_employee'] or d['uploaded_by'] == user.id)
    if not is_hr(user) and not own:
        raise NotFoundError('document not found')
    return d


def document_file(user: AuthUser, doc_id: str) -> dict:
    """The decrypted file of a document; HR opening someone else's document is audited."""
    company_id = company_id_of(user)
    d = document_for(user, doc_id)
    f = read_file(company_id, d['file_id'])
    if user.employee_id != d['employee_id']:
        audit('view_document', 'employee_document', actor_id=user.id, row_id=d['id'],
              after={'employeeId': d['employee_id'], 'title': d['title']})
    return f


def verify_document(user: AuthUser, doc_id: str, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    did = _uuid_param(doc_id)
    b = parse(VERIFY_BODY, body)
    if b['decision'] == 'reject' and not b.get('reason'):
        raise BadRequestError('give a reason so the employee knows what to fix')
    d = load_doc(company_id, did)
    if user.employee_id == d['employee_id']:
        raise ForbiddenError('you cannot verify your own documents')
    verify = b['decision'] == 'verify'
    with transaction.atomic():
        n = execute("""UPDATE employee_document SET status = %s, reject_reason = %s, verified_by = %s, verified_at = now()
                        WHERE id = %s AND status = 'pending'""",
                    ['verified' if verify else 'rejected', None if verify else b['reason'], user.id, did])
        if not n:
            raise ConflictError('this document is not waiting for verification')
        audit(b['decision'], 'employee_document', actor_id=user.id, row_id=did, after={'reason': b.get('reason')})
        notify(company_id=company_id, user_id=user_id_for_employee(d['employee_id']), employee_id=d['employee_id'],
               template=f"document.{'verified' if verify else 'rejected'}",
               title=f"{d['title']} verified" if verify else f"{d['title']} needs another look",
               body=(f"HR verified your {d['title']}." if verify
                     else f"HR could not accept your {d['title']}: {b['reason']}. Upload it again."),
               payload={'documentId': did})
    return {'ok': True}


def update_document(user: AuthUser, doc_id: str, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    did = _uuid_param(doc_id)
    b = parse(DOCUMENT_PATCH, body)
    before = load_doc(company_id, did)
    has_expiry, has_notes = 'expiryDate' in b, 'notes' in b
    with transaction.atomic():
        execute("""UPDATE employee_document SET title = coalesce(%s, title), expiry_date = CASE WHEN %s THEN %s::date ELSE expiry_date END,
                          visible_to_employee = coalesce(%s, visible_to_employee), notes = CASE WHEN %s THEN %s ELSE notes END
                    WHERE id = %s AND company_id = %s""",
                [b.get('title'), has_expiry, b.get('expiryDate'), b.get('visibleToEmployee'), has_notes, b.get('notes'),
                 did, company_id])
        # A new expiry date restarts its alerts.
        if has_expiry and b.get('expiryDate') != before['expiry_date']:
            execute('DELETE FROM document_expiry_alert WHERE document_id = %s', [did])
        audit('update', 'employee_document', actor_id=user.id, row_id=did,
              before={'title': before['title'], 'expiryDate': before['expiry_date'], 'visible': before['visible_to_employee']},
              after=b)
    return {'ok': True}


def delete_document(user: AuthUser, doc_id: str) -> dict:
    """HR deletes any document; an employee only their own upload that HR has not verified yet. The bytes are wiped."""
    company_id = company_id_of(user)
    did = _uuid_param(doc_id)
    d = load_doc(company_id, did)
    own_pending = user.employee_id == d['employee_id'] and d['uploaded_by'] == user.id and d['status'] != 'verified'
    if not is_hr(user) and not own_pending:
        if user.employee_id == d['employee_id']:
            raise ForbiddenError('only HR can delete a verified document')
        raise NotFoundError('document not found')
    with transaction.atomic():
        execute('UPDATE employee_document SET deleted_at = now(), deleted_by = %s, is_current = false WHERE id = %s', [user.id, did])
        # Deleting the latest version brings the previous one back.
        if d['is_current'] and d['replaces_id']:
            execute('UPDATE employee_document SET is_current = true WHERE id = %s AND deleted_at IS NULL', [d['replaces_id']])
        delete_file(company_id, d['file_id'])
        audit('delete', 'employee_document', actor_id=user.id, row_id=did,
              before={'employeeId': d['employee_id'], 'title': d['title'], 'version': d['version']})
    return {'ok': True}


# ---------------------------------------------------------------- letter settings and templates

def placeholders(user: AuthUser) -> list[dict]:
    _require_hr(user)
    return PLACEHOLDERS


def get_letter_settings(user: AuthUser) -> dict:
    _require_hr(user)
    return letter_settings(company_id_of(user))


def save_letter_settings(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = parse(SETTINGS_BODY, body)
    before = letter_settings(company_id)
    with transaction.atomic():
        execute("""INSERT INTO company_setting (company_id, key, value, updated_by) VALUES (%s, 'documents.letters', %s, %s)
                   ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
                [company_id, json.dumps(b), user.id])
        audit('update', 'company_setting', actor_id=user.id, row_id='documents.letters', before=before, after=b)
    return b


def list_templates(user: AuthUser) -> list[dict]:
    """HR sees every template; employees only the ones they may request."""
    company_id = company_id_of(user)
    ensure_defaults(company_id)
    if is_hr(user):
        return _rs(rows("""SELECT id, code, name, subject, body, requestable, visible_to_employee, is_builtin, active, updated_at
                             FROM letter_template WHERE company_id = %s ORDER BY active DESC, name""", [company_id]))
    return _rs(rows("""SELECT id, code, name, subject FROM letter_template WHERE company_id = %s AND active AND requestable
                        ORDER BY name""", [company_id]))


def get_template(user: AuthUser, template_id: str) -> dict:
    _require_hr(user)
    t = one("""SELECT id, code, name, subject, body, requestable, visible_to_employee, is_builtin, active, updated_at
                 FROM letter_template WHERE id = %s AND company_id = %s""", [_uuid_param(template_id), company_id_of(user)])
    if not t:
        raise NotFoundError('template not found')
    return _r(t)


def _check_placeholders(subject: str, body: str) -> None:
    bad = unknown_placeholders(subject, body)
    if bad:
        raise BadRequestError(f"Unknown placeholder{'' if len(bad) == 1 else 's'}: {', '.join(bad)}", {'unknown': bad})


def create_template(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = parse(TEMPLATE_BODY, body)
    _check_placeholders(b['subject'], b['body'])
    with transaction.atomic():
        r = one("""INSERT INTO letter_template (company_id, code, name, subject, body, requestable, visible_to_employee, updated_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [company_id, b['code'], b['name'], b['subject'], b['body'], b['requestable'], b['visibleToEmployee'], user.id])
        audit('create', 'letter_template', actor_id=user.id, row_id=r['id'], after={'code': b['code'], 'name': b['name']})
    return {'id': str(r['id'])}


def update_template(user: AuthUser, template_id: str, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    tid = _uuid_param(template_id)
    b = parse(TEMPLATE_PATCH, body)
    _check_placeholders(b.get('subject') or '', b.get('body') or '')
    before = one("""SELECT name, subject, body, requestable, visible_to_employee, active FROM letter_template
                     WHERE id = %s AND company_id = %s""", [tid, company_id])
    if not before:
        raise NotFoundError('template not found')
    with transaction.atomic():
        execute("""UPDATE letter_template SET name = coalesce(%s, name), subject = coalesce(%s, subject), body = coalesce(%s, body),
                          requestable = coalesce(%s, requestable), visible_to_employee = coalesce(%s, visible_to_employee),
                          active = coalesce(%s, active), updated_by = %s, updated_at = now() WHERE id = %s AND company_id = %s""",
                [b.get('name'), b.get('subject'), b.get('body'), b.get('requestable'), b.get('visibleToEmployee'), b.get('active'),
                 user.id, tid, company_id])
        audit('update', 'letter_template', actor_id=user.id, row_id=tid, before=before, after=b)
    return {'ok': True}


def build_letter(company_id: str, tpl: dict, employee_id: str, *, ref_no: str, purpose: str | None = None,
                 addressed_to: str | None = None, preview: bool = False) -> dict:
    """Build a letter for an employee; `preview` highlights missing values and shows a DRAFT watermark."""
    values = employee_letter_values(company_id, employee_id, ref_no=ref_no, purpose=purpose, addressed_to=addressed_to)
    if not values:
        raise NotFoundError('employee not found')
    missing: dict[str, None] = {}
    subject_html = render_subject(tpl['subject'], values, missing, preview)
    body = render_body(tpl['body'], values, mark=preview)
    for m in body['missing']:
        missing[m] = None
    head = company_head(company_id)
    s = letter_settings(company_id)
    html = render_letter_html(company=head, ref_no=ref_no, date_bs=values.get('today') or bs_label(npt_today()),
                              subject_html=subject_html, body_html=body['html'],
                              signatory={'name': s.get('signatoryName'), 'title': s.get('signatoryTitle')}, draft=preview)
    return {'html': html, 'missing': list(missing), 'subject': subject_text(subject_html)}


def preview_letter(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = parse(PREVIEW_BODY, body)
    if b.get('templateId'):
        t = one('SELECT subject, body FROM letter_template WHERE id = %s AND company_id = %s', [b['templateId'], company_id])
        if not t:
            raise NotFoundError('template not found')
        tpl = {'subject': b['subject'] if b.get('subject') is not None else t['subject'],
               'body': b['body'] if b.get('body') is not None else t['body']}
    else:
        if not b.get('subject') or not b.get('body'):
            raise BadRequestError('subject and body are required')
        tpl = {'subject': b['subject'], 'body': b['body']}
    unknown = unknown_placeholders(tpl['subject'], tpl['body'])
    out = build_letter(company_id, tpl, b['employeeId'], ref_no='DRAFT', purpose=b.get('purpose'),
                       addressed_to=b.get('addressedTo'), preview=True)
    return {'html': out['html'], 'missing': [m for m in out['missing'] if m != 'ref_no'], 'unknown': unknown}


# ---------------------------------------------------------------- issued letters

def issue_letter(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = parse(ISSUE_BODY, body)
    ensure_defaults(company_id)
    request_id = b.get('requestId')
    with transaction.atomic():
        tpl = one('SELECT id, code, subject, body, active, visible_to_employee FROM letter_template WHERE id = %s AND company_id = %s',
                  [b['templateId'], company_id])
        if not tpl or not tpl['active']:
            raise NotFoundError('template not found')
        _assert_employee_in_company(company_id, b['employeeId'])
        purpose, addressed_to = b.get('purpose'), b.get('addressedTo')
        if request_id:
            q = one("""SELECT employee_id, status, purpose, addressed_to, workflow_instance_id FROM letter_request
                        WHERE id = %s AND company_id = %s FOR UPDATE""", [request_id, company_id])
            if not q:
                raise NotFoundError('letter request not found')
            if str(q['employee_id']) != b['employeeId']:
                raise BadRequestError('the request belongs to another employee')
            if q['status'] not in ('pending', 'approved'):
                raise ConflictError(f"request already {q['status']}")
            purpose = purpose if purpose is not None else q['purpose']
            addressed_to = addressed_to if addressed_to is not None else q['addressed_to']
            if q['status'] == 'pending' and q['workflow_instance_id']:
                act(instance_id=str(q['workflow_instance_id']), user=user, decision='approve')
        s = letter_settings(company_id)
        # Check for missing values before taking a reference number.
        probe = build_letter(company_id, tpl, b['employeeId'], ref_no='X', purpose=purpose, addressed_to=addressed_to)
        if probe['missing'] and not b['allowMissing']:
            raise BadRequestError(f"Fill in these details first: {', '.join(probe['missing'])}",
                                  {'code': 'missing_values', 'missing': probe['missing']})
        ref_no = next_ref_no(company_id, s.get('refPrefix') or '')
        letter = build_letter(company_id, tpl, b['employeeId'], ref_no=ref_no, purpose=purpose, addressed_to=addressed_to)
        visible = b['visibleToEmployee'] if b.get('visibleToEmployee') is not None else tpl['visible_to_employee']
        r = one("""INSERT INTO issued_letter (company_id, employee_id, template_id, template_code, ref_no, subject, html,
                                              visible_to_employee, request_id, issued_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [company_id, b['employeeId'], tpl['id'], tpl['code'], ref_no, letter['subject'], letter['html'], visible,
                 request_id, user.id])
        letter_id = str(r['id'])
        if request_id:
            execute("""UPDATE letter_request SET status = 'issued', issued_letter_id = %s, decided_at = coalesce(decided_at, now())
                        WHERE id = %s""", [letter_id, request_id])
        audit('issue', 'issued_letter', actor_id=user.id, row_id=letter_id,
              after={'employeeId': b['employeeId'], 'template': tpl['code'], 'refNo': ref_no, 'requestId': request_id})
        if visible:
            notify(company_id=company_id, user_id=user_id_for_employee(b['employeeId']), employee_id=b['employeeId'],
                   template='letter.issued', title=f"{letter['subject']} issued",
                   body=f'HR issued your letter "{letter["subject"]}" ({ref_no}). Open it under Profile, Documents & letters.',
                   payload={'letterId': letter_id}, channels=('push',))
    return {'id': letter_id, 'refNo': ref_no}


def list_letters(user: AuthUser, query: dict | None = None) -> list[dict]:
    company_id = company_id_of(user)
    q = _q(LETTERS_QUERY, query)
    hr = is_hr(user)
    if not hr and q.get('employeeId') and q['employeeId'] != user.employee_id:
        raise ForbiddenError('you can only see your own letters')
    employee_id = q.get('employeeId') if hr else user.employee_id
    if not hr and not employee_id:
        return []
    term = (q.get('q') or '').strip() or None
    return _rs(rows("""
        SELECT l.id, l.employee_id, e.emp_code, e.name_en, l.template_code, l.ref_no, l.subject, l.visible_to_employee, l.request_id,
               l.issued_at, l.voided_at, l.void_reason, iu.name AS issued_by_name
          FROM issued_letter l JOIN employee e ON e.id = l.employee_id LEFT JOIN app_user iu ON iu.id = l.issued_by
         WHERE l.company_id = %(c)s AND (%(e)s::uuid IS NULL OR l.employee_id = %(e)s::uuid)
           AND (%(hr)s OR (l.visible_to_employee AND l.voided_at IS NULL))
           AND (%(q)s::text IS NULL OR l.ref_no ILIKE '%%' || %(q)s || '%%' OR e.name_en ILIKE '%%' || %(q)s || '%%'
                OR e.emp_code ILIKE '%%' || %(q)s || '%%' OR l.subject ILIKE '%%' || %(q)s || '%%')
         ORDER BY l.issued_at DESC LIMIT %(lim)s""",
        {'c': company_id, 'e': employee_id, 'hr': hr, 'q': term, 'lim': q['limit']}))


def letter_html(user: AuthUser, letter_id: str) -> str:
    company_id = company_id_of(user)
    lid = _uuid_param(letter_id)
    row = one('SELECT employee_id, html, visible_to_employee, voided_at, void_reason FROM issued_letter WHERE id = %s AND company_id = %s',
              [lid, company_id])
    own = bool(row) and user.employee_id == str(row['employee_id']) and row['visible_to_employee'] and not row['voided_at']
    if not row or (not is_hr(user) and not own):
        raise NotFoundError('letter not found')
    html = row['html']
    if row['voided_at']:
        html = html.replace('<body>', '<body><div class="wm">VOID</div>', 1).replace(
            '</body>', f'<p class="foot">Voided: {esc(row["void_reason"] or "")}</p></body>', 1)
    if user.employee_id != str(row['employee_id']):
        audit('view_letter', 'issued_letter', actor_id=user.id, row_id=lid)
    return html


def void_letter(user: AuthUser, letter_id: str, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    lid = _uuid_param(letter_id)
    b = parse(VOID_BODY, body)
    with transaction.atomic():
        n = execute("""UPDATE issued_letter SET voided_at = now(), voided_by = %s, void_reason = %s
                        WHERE id = %s AND company_id = %s AND voided_at IS NULL""", [user.id, b['reason'], lid, company_id])
        if not n:
            raise NotFoundError('letter not found or already voided')
        audit('void', 'issued_letter', actor_id=user.id, row_id=lid, after=b)
    return {'ok': True}


# ---------------------------------------------------------------- letter requests

def request_letter(user: AuthUser, body: dict) -> dict:
    company_id = company_id_of(user)
    if not user.employee_id:
        raise BadRequestError('only employees can request letters')
    employee_id = user.employee_id
    b = parse(REQUEST_BODY, body)
    ensure_defaults(company_id)
    t0 = one('SELECT name FROM letter_template WHERE id = %s AND company_id = %s AND active AND requestable',
             [b['templateId'], company_id])
    if not t0:
        raise BadRequestError('this letter cannot be requested')
    name = t0['name']
    if one("""SELECT 1 FROM letter_request WHERE employee_id = %s AND template_id = %s AND status IN ('pending', 'approved')""",
           [employee_id, b['templateId']]):
        raise ConflictError(f'you already have an open request for a {name.lower()}')
    emp = _assert_employee_in_company(company_id, employee_id)
    with transaction.atomic():
        r = one("""INSERT INTO letter_request (company_id, employee_id, template_id, purpose, addressed_to, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                [company_id, employee_id, b['templateId'], b['purpose'], b.get('addressedTo'), user.id])
        req_id = str(r['id'])
        inst = start_workflow(company_id=company_id, object_type='letter_request', object_id=req_id, employee_id=employee_id,
                              title=f'{name} requested', body=f"{emp['name_en']} asked for a {name.lower()}: {b['purpose']}")
        execute('UPDATE letter_request SET workflow_instance_id = %s WHERE id = %s', [inst['id'], req_id])
        audit('create', 'letter_request', actor_id=user.id, row_id=req_id, after=b)
    return {'id': req_id}


def list_requests(user: AuthUser, query: dict | None = None) -> list[dict]:
    company_id = company_id_of(user)
    q = _q(REQUESTS_QUERY, query)
    hr = is_hr(user)
    if not hr and not user.employee_id:
        return []
    return _rs(rows("""
        SELECT lr.id, lr.employee_id, e.emp_code, e.name_en, lr.template_id, t.name AS template_name, t.code AS template_code,
               lr.purpose, lr.addressed_to, lr.status, lr.decision_reason, lr.issued_letter_id, lr.created_at, lr.decided_at
          FROM letter_request lr JOIN employee e ON e.id = lr.employee_id JOIN letter_template t ON t.id = lr.template_id
         WHERE lr.company_id = %(c)s AND (%(hr)s OR lr.employee_id = %(e)s::uuid)
           AND (%(s)s::text IS NULL OR (%(s)s = 'open' AND lr.status IN ('pending', 'approved')) OR lr.status = %(s)s)
         ORDER BY lr.created_at DESC LIMIT 300""",
        {'c': company_id, 'hr': hr, 'e': user.employee_id, 's': q.get('status')}))


def get_request(user: AuthUser, request_id: str) -> dict:
    """One letter request, for HR preparing the letter."""
    _require_hr(user)
    r = one("""SELECT lr.id, lr.employee_id, e.emp_code, e.name_en, lr.template_id, t.name AS template_name, t.code AS template_code,
                      lr.purpose, lr.addressed_to, lr.status
                 FROM letter_request lr JOIN employee e ON e.id = lr.employee_id JOIN letter_template t ON t.id = lr.template_id
                WHERE lr.id = %s AND lr.company_id = %s""", [_uuid_param(request_id), company_id_of(user)])
    if not r:
        raise NotFoundError('letter request not found')
    return _r(r)


def cancel_request(user: AuthUser, request_id: str) -> dict:
    company_id = company_id_of(user)
    rid = _uuid_param(request_id)
    row = one('SELECT employee_id, status FROM letter_request WHERE id = %s AND company_id = %s', [rid, company_id])
    if not row or (str(row['employee_id']) != user.employee_id and not is_hr(user)):
        raise NotFoundError('request not found')
    if row['status'] not in ('pending', 'approved'):
        raise ConflictError(f"request already {row['status']}")
    with transaction.atomic():
        execute("UPDATE letter_request SET status = 'cancelled', decided_at = now() WHERE id = %s", [rid])
        cancel_workflow('letter_request', rid)
        audit('cancel', 'letter_request', actor_id=user.id, row_id=rid)
    return {'ok': True}


# ---------------------------------------------------------------- company policies

def list_policies(user: AuthUser, query: dict | None = None) -> list[dict]:
    company_id = company_id_of(user)
    q = _q(ARCHIVED_QUERY, query)
    if is_hr(user):
        ps = _rs(rows("""
            SELECT p.id, p.title, p.description, p.audience, p.audience_ids, p.requires_ack, p.published_at, p.archived_at, f.filename,
                   (SELECT count(*)::int FROM policy_ack a JOIN employee e ON e.id = a.employee_id
                     WHERE a.policy_id = p.id AND e.status = 'active') AS acks
              FROM company_policy p JOIN stored_file f ON f.id = p.file_id
             WHERE p.company_id = %s AND (%s OR p.archived_at IS NULL) ORDER BY p.archived_at NULLS FIRST, p.published_at DESC""",
            [company_id, q.get('archived') == '1']))
        out = []
        for p in ps:
            ids = audience_ids(company_id, p)
            my_ack = None
            if user.employee_id:
                mine = one('SELECT acknowledged_at FROM policy_ack WHERE policy_id = %s AND employee_id = %s', [p['id'], user.employee_id])
                my_ack = mine['acknowledged_at'] if mine else None
            out.append({**p, 'audience_size': len(ids), 'my_ack_at': my_ack,
                        'in_audience': bool(user.employee_id) and not p['archived_at'] and user.employee_id in ids})
        return out
    if not user.employee_id:
        return []
    ps = _rs(rows("""
        SELECT p.id, p.title, p.description, p.audience, p.audience_ids, p.requires_ack, p.published_at, f.filename,
               (SELECT acknowledged_at FROM policy_ack a WHERE a.policy_id = p.id AND a.employee_id = %s) AS my_ack_at
          FROM company_policy p JOIN stored_file f ON f.id = p.file_id
         WHERE p.company_id = %s AND p.archived_at IS NULL ORDER BY p.published_at DESC""", [user.employee_id, company_id]))
    out = []
    for p in ps:
        if audience_ids(company_id, p, user.employee_id):
            out.append({'id': p['id'], 'title': p['title'], 'description': p['description'], 'requires_ack': p['requires_ack'],
                        'published_at': p['published_at'], 'filename': p['filename'], 'my_ack_at': p['my_ack_at'],
                        'in_audience': True})
    return out


def publish_policy(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = parse(POLICY_BODY, body)
    aud, aud_ids = b['audience'], b['audienceIds']
    if aud != 'all' and not aud_ids:
        raise BadRequestError(f'choose at least one {aud}')
    if aud != 'all':
        table = 'branch' if aud == 'branch' else 'org_unit'
        n = one(f'SELECT count(*)::int AS n FROM {table} WHERE company_id = %s AND id = ANY(%s::uuid[])', [company_id, aud_ids])['n']
        if n != len(set(aud_ids)):
            raise BadRequestError(f'unknown {aud}')
    stored_ids = [] if aud == 'all' else aud_ids
    with transaction.atomic():
        r = one("""INSERT INTO company_policy (company_id, title, description, file_id, audience, audience_ids, requires_ack, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s::uuid[], %s, %s) RETURNING id""",
                [company_id, b['title'], b.get('description'), b['fileId'], aud, stored_ids, b['requiresAck'], user.id])
        pid = str(r['id'])
        attach_file(company_id=company_id, file_id=b['fileId'], user_id=user.id, purpose='company_document',
                    attached_to=f'company_policy:{pid}')
        for employee_id in audience_ids(company_id, {'audience': aud, 'audience_ids': stored_ids}):
            user_id = user_id_for_employee(employee_id)
            if not user_id:
                continue
            notify(company_id=company_id, user_id=user_id, employee_id=employee_id, template='policy.published',
                   title=f"Please read: {b['title']}" if b['requiresAck'] else f"New company document: {b['title']}",
                   body=(f"{b['title']} has been published. Read it and acknowledge it under Profile, Documents & letters."
                         if b['requiresAck'] else f"{b['title']} is available under Profile, Documents & letters."),
                   payload={'policyId': pid})
            if b['requiresAck']:
                execute('INSERT INTO policy_nudge (policy_id, employee_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', [pid, employee_id])
        audit('create', 'company_policy', actor_id=user.id, row_id=pid,
              after={'title': b['title'], 'audience': aud, 'requiresAck': b['requiresAck']})
    return {'id': pid}


def update_policy(user: AuthUser, policy_id: str, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    pid = _uuid_param(policy_id)
    b = parse(POLICY_PATCH, body)
    with transaction.atomic():
        n = execute("""UPDATE company_policy SET title = coalesce(%s, title), description = CASE WHEN %s THEN %s ELSE description END,
                              requires_ack = coalesce(%s, requires_ack),
                              archived_at = CASE WHEN %s::boolean IS NULL THEN archived_at WHEN %s THEN coalesce(archived_at, now()) ELSE NULL END
                        WHERE id = %s AND company_id = %s""",
                    [b.get('title'), 'description' in b, b.get('description'), b.get('requiresAck'), b.get('archived'),
                     b.get('archived'), pid, company_id])
        if not n:
            raise NotFoundError('policy not found')
        audit('update', 'company_policy', actor_id=user.id, row_id=pid, after=b)
    return {'ok': True}


def _load_policy_for(user: AuthUser, policy_id: str) -> tuple[str, dict, bool]:
    company_id = company_id_of(user)
    row = _r(one("""SELECT id, title, file_id, audience, audience_ids, requires_ack, archived_at FROM company_policy
                     WHERE id = %s AND company_id = %s""", [_uuid_param(policy_id), company_id]))
    if not row:
        raise NotFoundError('policy not found')
    member = bool(user.employee_id) and not row['archived_at'] and len(audience_ids(company_id, row, user.employee_id)) > 0
    if not is_hr(user) and not member:
        raise NotFoundError('policy not found')
    return company_id, row, member


def get_policy(user: AuthUser, policy_id: str) -> dict:
    return _load_policy_for(user, policy_id)[1]


def policy_file(user: AuthUser, policy_id: str) -> dict:
    company_id, row, _ = _load_policy_for(user, policy_id)
    return read_file(company_id, row['file_id'])


def acknowledge_policy(user: AuthUser, policy_id: str) -> dict:
    _, row, member = _load_policy_for(user, policy_id)
    if not member:
        raise ForbiddenError('this policy is not addressed to you')
    execute('INSERT INTO policy_ack (policy_id, employee_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', [row['id'], user.employee_id])
    return {'ok': True}


def policy_acks(user: AuthUser, policy_id: str) -> list[dict]:
    _require_hr(user)
    company_id, row, _ = _load_policy_for(user, policy_id)
    ids = audience_ids(company_id, row)
    return _rs(rows("""
        SELECT e.id AS employee_id, e.emp_code, e.name_en, a.acknowledged_at, n.last_sent_at AS last_reminded_at
          FROM employee e LEFT JOIN policy_ack a ON a.policy_id = %s AND a.employee_id = e.id
          LEFT JOIN policy_nudge n ON n.policy_id = %s AND n.employee_id = e.id
         WHERE e.id = ANY(%s::uuid[]) ORDER BY a.acknowledged_at NULLS FIRST, e.emp_code""", [row['id'], row['id'], ids]))


def remind_policy(user: AuthUser, policy_id: str) -> dict:
    _require_hr(user)
    company_id, row, _ = _load_policy_for(user, policy_id)
    if row['archived_at']:
        raise ConflictError('this policy is archived')
    ids = audience_ids(company_id, row)
    pending = rows("""SELECT x AS id FROM unnest(%s::uuid[]) x
                       WHERE NOT EXISTS (SELECT 1 FROM policy_ack a WHERE a.policy_id = %s AND a.employee_id = x)""", [ids, row['id']])
    sent = 0
    for p in pending:
        employee_id = str(p['id'])
        user_id = user_id_for_employee(employee_id)
        if not user_id:
            continue
        with transaction.atomic():
            notify(company_id=company_id, user_id=user_id, employee_id=employee_id, template='policy.reminder',
                   title=f"Please read: {row['title']}",
                   body=f"{row['title']} is waiting for your acknowledgement under Profile, Documents & letters.",
                   payload={'policyId': row['id']})
            execute("""INSERT INTO policy_nudge (policy_id, employee_id) VALUES (%s, %s)
                       ON CONFLICT (policy_id, employee_id) DO UPDATE SET last_sent_at = now()""", [row['id'], employee_id])
        sent += 1
    return {'sent': sent}


# ---------------------------------------------------------------- pickers for the pages

def branches(user: AuthUser) -> list[dict]:
    return _rs(rows('SELECT id, name_en FROM branch WHERE company_id = %s ORDER BY name_en', [company_id_of(user)]))


def org_units(user: AuthUser) -> list[dict]:
    return _rs(rows('SELECT id, name_en FROM org_unit WHERE company_id = %s ORDER BY name_en', [company_id_of(user)]))
