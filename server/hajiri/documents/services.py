"""
Documents & letters (port of apps/api/src/modules/documents/routes.ts): employee documents with verification,
versions and expiry alerts; letter templates, issued letters with reference numbers and letter requests (through the
workflow engine); company policies with acknowledgements and reminders.

Every public function takes the signed-in AuthUser and, where the TS route had one, a body or query in the TS API's
shape (camelCase keys), validated by the serializers in serializers.py with the same rules and messages as the zod
schemas. Every query is scoped to the user's ACTIVE company; another company's id is simply not found. Errors are the
TS ones.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from django.db import models, transaction
from django.db.models import Count, Exists, F, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce, Now

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import now as clock_now
from hajiri.core.clock import npt_today as npt_date
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.settings_store import put_setting
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (
    Branch, CompanyPolicy, DocumentCategory, DocumentExpiryAlert, Employee, EmployeeAssignment, EmployeeDocument,
    IssuedLetter, LetterRequest, LetterTemplate, OrgUnit, PolicyAck, PolicyNudge,
)
from hajiri.files.store import attach_file, delete_file, read_file
from hajiri.notifications.service import notify, notify_role, user_id_for_employee
from hajiri.workflow.service import DecisionContext, act, cancel_workflow, register_handler, start_workflow

from .filters import OPEN_REQUEST, LetterFilter, LetterRequestFilter
from .letters import (PLACEHOLDERS, SETTINGS_KEY, company_head, employee_letter_values, esc, letter_settings,
                      next_ref_no, npt_today, render_body, render_letter_html, render_subject, seed_document_defaults,
                      subject_text, unknown_placeholders)
from .serializers import (
    PolicyArchivedQuery, DocumentCategoryIn, DocumentCategoryPatchIn, DocumentIn, DocumentPatchIn, DocumentHistoryQuery, LetterIssueIn, LetterRequestIn,
    LettersQuery, LetterSettingsIn, CompanyPolicyIn, CompanyPolicyPatchIn, LetterPreviewIn, DocumentQueueQuery, RequestsQuery, LetterTemplateIn,
    LetterTemplatePatchIn, DocumentVerifyIn, LetterVoidIn,
)

DocStatus = EmployeeDocument.Status
ReqStatus = LetterRequest.Status
Audience = CompanyPolicy.Audience
ACTIVE = Employee.Status.ACTIVE

# ---------------------------------------------------------------- shared helpers

_seeded: set[str] = set()


def ensure_defaults(company_id: str) -> None:
    """Companies created before this module get their categories and built-in templates on first use."""
    if company_id in _seeded:
        return
    seed_document_defaults(company_id)
    _seeded.add(company_id)


def is_hr(u: AuthUser) -> bool:
    return has_role(u, 'hr')


def _require_hr(u: AuthUser) -> None:
    if not is_hr(u):
        raise ForbiddenError('requires role hr')


def _today() -> date:
    return date.fromisoformat(npt_today())


def _q(serializer, query: dict | None) -> dict:
    """A query string as zod saw it: empty values count as absent."""
    return plain(validate(serializer, {k: v for k, v in (query or {}).items() if v not in (None, '')}))


def _body(serializer, body: Any) -> dict:
    return plain(validate(serializer, body))


def employee_row(company_id: str, employee_id: str) -> dict:
    """Name, code and status of an employee of the company; NotFoundError otherwise."""
    r = Employee.objects.for_company(company_id).filter(pk=employee_id).values('name_en', 'emp_code', 'status').first()
    if not r:
        raise NotFoundError('employee not found')
    return r


def _set_if(fields: dict, key: str, value: Any) -> None:
    """coalesce($n, column): only a value that was given replaces the column."""
    if value is not None:
        fields[key] = value


# ---------------------------------------------------------------- policy audience

def _unit_children(company_id: str) -> dict[str, list[str]]:
    """The company's unit tree as parent id -> child ids."""
    children: dict[str, list[str]] = defaultdict(list)
    for uid, parent in OrgUnit.objects.for_company(company_id).values_list('id', 'parent_id'):
        if parent:
            children[str(parent)].append(str(uid))
    return children


def _units_under(company_id: str, roots: list[str], children: dict[str, list[str]] | None = None) -> set[str]:
    """The org units at or below `roots` (sub-units up to 20 levels down), from the company's unit tree."""
    if children is None:
        children = _unit_children(company_id)
    found = {str(r) for r in roots}
    frontier = list(found)
    for _ in range(20):
        frontier = [c for u in frontier for c in children.get(u, ()) if c not in found]
        if not frontier:
            break
        found.update(frontier)
    return found


def audience_queryset(company_id: str, p: dict, only_employee: str | None = None) -> models.QuerySet:
    """Active employees a policy is addressed to: everyone, chosen branches, or chosen departments (with sub-units),
    by today's assignment."""
    qs = Employee.objects.for_company(company_id).filter(status=ACTIVE)
    if only_employee:
        qs = qs.filter(pk=only_employee)
    if p['audience'] == Audience.ALL:
        return qs
    ids = [str(x) for x in (p.get('audience_ids') or [])]
    if not ids:
        return qs.none()
    current = EmployeeAssignment.objects.filter(employee=OuterRef('pk'), valid__contains=_today())
    if p['audience'] == Audience.BRANCH:
        return qs.filter(Exists(current.filter(branch_id__in=ids)))
    return qs.filter(Exists(current.filter(org_unit_id__in=_units_under(company_id, ids))))


def audience_ids(company_id: str, p: dict, only_employee: str | None = None) -> list[str]:
    return [str(x) for x in audience_queryset(company_id, p, only_employee).order_by('id').values_list('id', flat=True)]


class AudienceIndex:
    """The audiences of many policies of one company from one read of its active employees and today's assignments
    (and the unit tree, when a department audience asks for it), so a list of policies does not query per policy.
    Same rules as audience_queryset."""

    def __init__(self, company_id: str, only_employee: str | None = None):
        self.company_id = company_id
        people = Employee.objects.for_company(company_id).filter(status=ACTIVE)
        if only_employee:
            people = people.filter(pk=only_employee)
        self.everyone = [str(x) for x in people.order_by('id').values_list('id', flat=True)]
        self.by_branch: dict[str, set[str]] = defaultdict(set)
        self.by_unit: dict[str, set[str]] = defaultdict(set)
        assignments = (EmployeeAssignment.objects.filter(employee__in=people, valid__contains=_today())
                       .values_list('employee_id', 'branch_id', 'org_unit_id'))
        for employee_id, branch_id, unit_id in assignments:
            if branch_id:
                self.by_branch[str(branch_id)].add(str(employee_id))
            if unit_id:
                self.by_unit[str(unit_id)].add(str(employee_id))
        self._children: dict[str, list[str]] | None = None

    def ids(self, p: dict) -> list[str]:
        if p['audience'] == Audience.ALL:
            return self.everyone
        wanted = [str(x) for x in (p.get('audience_ids') or [])]
        if not wanted:
            return []
        if p['audience'] == Audience.BRANCH:
            groups = [self.by_branch.get(b, ()) for b in wanted]
        else:
            if self._children is None:
                self._children = _unit_children(self.company_id)
            groups = [self.by_unit.get(u, ()) for u in _units_under(self.company_id, wanted, self._children)]
        members = set().union(*groups)
        return [e for e in self.everyone if e in members]


# ---------------------------------------------------------------- letter requests (workflow)

def apply_letter_request_decision(request_id: str, state: str, ctx: DecisionContext) -> None:
    """The workflow engine's handler: record HR's decision on a letter request (the row is locked first)."""
    lr = (LetterRequest.objects.select_for_update(of=('self',)).filter(pk=request_id)
          .values('employee_id', 'company_id', 'status', name=F('template__name')).first())
    if not lr:
        raise NotFoundError('letter request not found')
    if lr['status'] != ReqStatus.PENDING:
        raise ConflictError(f"request already {lr['status']}")
    LetterRequest.objects.filter(pk=request_id).update(status=state, decision_reason=ctx.reason, decided_at=Now())
    if state == ReqStatus.REJECTED:
        emp = str(lr['employee_id'])
        notify(company_id=str(lr['company_id']), user_id=user_id_for_employee(emp), employee_id=emp,
               template='letter_request.rejected', title=f"{lr['name']} request declined",
               body=f"HR declined your request for a {lr['name'].lower()}{': ' + ctx.reason if ctx.reason else '.'}",
               payload={'requestId': str(request_id)}, channels=('push',))


register_handler('letter_request', apply_letter_request_decision)


# ---------------------------------------------------------------- daily job

def _expiry_when(expiry: str, days: int) -> str:
    if days < 0:
        return f'expired on {bs_label(expiry)}'
    if days == 0:
        return 'expires today'
    return f'expires on {bs_label(expiry)} (in {days} days)'


def _expiry_alerts(today: date) -> int:
    """Alerts at 30, 7 and 0 days before a document expires: one per document and threshold to the employee (when
    they can see it), and one summary per company to HR."""
    docs = (EmployeeDocument.objects
            .filter(deleted_at__isnull=True, is_current=True, employee__status=ACTIVE,
                    expiry_date__range=(today - timedelta(days=30), today + timedelta(days=30)))
            .exclude(status=DocStatus.REJECTED)
            .order_by('expiry_date', 'id')
            .values('id', 'company_id', 'employee_id', 'title', 'expiry_date', 'visible_to_employee',
                    name_en=F('employee__name_en')))
    sent = 0
    per_company: dict[str, list[str]] = {}
    for d in docs:
        days = (d['expiry_date'] - today).days
        expiry = d['expiry_date'].isoformat()
        threshold = 0 if days <= 0 else 7 if days <= 7 else 30
        when = _expiry_when(expiry, days)
        with transaction.atomic():
            _, created = DocumentExpiryAlert.objects.get_or_create(document_id=d['id'], threshold=threshold)
            if not created:
                continue
            sent += 1
            employee_id, company_id = str(d['employee_id']), str(d['company_id'])
            if d['visible_to_employee']:
                user_id = user_id_for_employee(employee_id)
                if user_id:
                    notify(company_id=company_id, user_id=user_id, employee_id=employee_id, template='document.expiring',
                           title=f"{d['title']} {'has expired' if days < 0 else 'is expiring'}",
                           body=f"Your {d['title']} {when}. Upload the renewed document under Profile, Documents & letters.",
                           payload={'documentId': str(d['id']), 'threshold': threshold}, channels=('push',))
        per_company.setdefault(company_id, []).append(f"{d['name_en']}: {d['title']} {when}")
    for company_id, lines in per_company.items():
        notify_role(company_id, 'hr', template='document.expiring.summary',
                    title=f"{len(lines)} employee document{'' if len(lines) == 1 else 's'} expiring",
                    body='; '.join(lines[:10]) + (f'; and {len(lines) - 10} more' if len(lines) > 10 else ''),
                    payload={'count': len(lines)})
    return sent


def _remind(company_id: str, policy: dict, employee_id: str, at=None) -> bool:
    """One acknowledgement reminder, and the nudge time recorded. False when the employee has no sign-in."""
    user_id = user_id_for_employee(employee_id)
    if not user_id:
        return False
    with transaction.atomic():
        notify(company_id=company_id, user_id=user_id, employee_id=employee_id, template='policy.reminder',
               title=f"Please read: {policy['title']}",
               body=f"{policy['title']} is waiting for your acknowledgement under Profile, Documents & letters.",
               payload={'policyId': str(policy['id'])})
        PolicyNudge.objects.bulk_create([PolicyNudge(policy_id=policy['id'], employee_id=employee_id,
                                                     last_sent_at=at or clock_now())],
                                        update_conflicts=True, unique_fields=['policy', 'employee'],
                                        update_fields=['last_sent_at'])
    return True


def _acknowledged(policy_id, employee_ids) -> set[str]:
    return {str(x) for x in PolicyAck.objects.filter(policy_id=policy_id, employee_id__in=employee_ids)
            .values_list('employee_id', flat=True)}


def _policy_nudges(now: datetime) -> int:
    """A weekly reminder to everyone in the audience of a policy that needs acknowledgement and is two days old."""
    sent = 0
    policies = (CompanyPolicy.objects.filter(archived_at__isnull=True, requires_ack=True,
                                             published_at__lt=now - timedelta(days=2))
                .order_by('published_at', 'id').values('id', 'company_id', 'title', 'audience', 'audience_ids'))
    audiences: dict[str, AudienceIndex] = {}
    for p in policies:
        cid = str(p['company_id'])
        if cid not in audiences:
            audiences[cid] = AudienceIndex(cid)
        ids = audiences[cid].ids(p)
        if not ids:
            continue
        done = _acknowledged(p['id'], ids)
        recent = {str(x) for x in PolicyNudge.objects.filter(policy_id=p['id'], employee_id__in=ids,
                                                             last_sent_at__gt=now - timedelta(days=7))
                  .values_list('employee_id', flat=True)}
        for employee_id in ids:
            if employee_id not in done and employee_id not in recent and _remind(cid, p, employee_id, now):
                sent += 1
    return sent


def documents_daily_job(now: datetime | None = None) -> dict:
    """
    Expiry alerts at 30, 7 and 0 days before a document expires (one per document and threshold; HR gets one summary
    per company), and a weekly reminder to employees who have not acknowledged a policy that needs acknowledgement.
    A background job: it works through every company, each step scoped by its row's company.
    """
    now = now or clock_now()
    today = npt_date(now)
    return {'expiryAlerts': _expiry_alerts(today), 'policyNudges': _policy_nudges(now)}


# ---------------------------------------------------------------- categories

CATEGORY_FIELDS = ('id', 'code', 'name', 'employee_can_upload', 'expiry_required', 'visible_to_employee', 'sort_order',
                   'active')


def list_categories(user: AuthUser) -> list[dict]:
    company_id = company_id_of(user)
    ensure_defaults(company_id)
    qs = DocumentCategory.objects.for_company(company_id)
    if not is_hr(user):
        qs = qs.filter(active=True)
    return [plain(r) for r in qs.order_by('sort_order', 'name').values(*CATEGORY_FIELDS)]


def create_category(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = _body(DocumentCategoryIn, body)
    with transaction.atomic():
        c = DocumentCategory.objects.create(
            company_id=company_id, code=b['code'], name=b['name'], employee_can_upload=b['employeeCanUpload'],
            expiry_required=b['expiryRequired'], visible_to_employee=b['visibleToEmployee'], sort_order=b['sortOrder'])
        audit('create', 'document_category', actor_id=user.id, row_id=c.id, after=b)
    return {'id': str(c.id)}


_CATEGORY_COLUMNS = {'name': 'name', 'employeeCanUpload': 'employee_can_upload', 'expiryRequired': 'expiry_required',
                     'visibleToEmployee': 'visible_to_employee', 'sortOrder': 'sort_order', 'active': 'active'}


def update_category(user: AuthUser, category_id: str, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    cid = parse_uuid(category_id)
    b = _body(DocumentCategoryPatchIn, body)
    fields: dict = {}
    for key, column in _CATEGORY_COLUMNS.items():
        _set_if(fields, column, b.get(key))
    with transaction.atomic():
        qs = DocumentCategory.objects.for_company(company_id).filter(pk=cid)
        if not (qs.update(**fields) if fields else qs.exists()):
            raise NotFoundError('category not found')
        audit('update', 'document_category', actor_id=user.id, row_id=cid, after=b)
    return {'ok': True}


# ---------------------------------------------------------------- employee documents

DOC_FIELDS = ('id', 'company_id', 'employee_id', 'category_id', 'title', 'file_id', 'version', 'replaces_id',
              'is_current', 'status', 'reject_reason', 'expiry_date', 'visible_to_employee', 'notes', 'uploaded_by_id',
              'verified_at', 'created_at')


def _documents(company_id: str) -> models.QuerySet:
    """The company's documents that have not been deleted."""
    return EmployeeDocument.objects.for_company(company_id).filter(deleted_at__isnull=True)


def _doc_values(qs: models.QuerySet, *extra, **annotations) -> models.QuerySet:
    """DOC_COLS of the TS route: the document with its category name, uploader name and file details."""
    return qs.values(*DOC_FIELDS, *extra, category_name=F('category__name'), uploaded_by_name=F('uploaded_by__name'),
                     filename=F('file__filename'), mime=F('file__mime'), bytes=F('file__bytes'), **annotations)


def _doc_row(r: dict, today: date | None = None) -> dict:
    r = plain(r)
    r['uploaded_by'] = r.pop('uploaded_by_id')
    expiry = r['expiry_date']
    r['expiry_date'] = expiry.isoformat() if expiry else None
    if today is not None:
        r['days_left'] = (expiry - today).days if expiry else None
    return r


def _list_docs(user: AuthUser, employee_id: str, include_history: bool) -> list[dict]:
    """Who may see an employee's documents: HR all of them; the employee their own visible ones and their own uploads."""
    company_id = company_id_of(user)
    hr = is_hr(user)
    if not hr and user.employee_id != employee_id:
        raise ForbiddenError('you can only see your own documents')
    employee_row(company_id, employee_id)
    qs = _documents(company_id).filter(employee_id=employee_id)
    if not (include_history and hr):
        qs = qs.filter(is_current=True)
    if not hr:
        qs = qs.filter(Q(visible_to_employee=True) | Q(uploaded_by_id=user.id))
    qs = qs.order_by('category__sort_order', 'category__name', 'title', '-version')
    return [_doc_row(r) for r in _doc_values(qs)]


def my_documents(user: AuthUser) -> list[dict]:
    if not user.employee_id:
        return []
    ensure_defaults(company_id_of(user))
    return _list_docs(user, user.employee_id, False)


def employee_documents(user: AuthUser, employee_id: str, query: dict | None = None) -> list[dict]:
    eid = parse_uuid(employee_id)
    q = _q(DocumentHistoryQuery, query)
    return _list_docs(user, eid, q.get('history') == '1')


def queue(user: AuthUser, query: dict | None = None) -> dict:
    """HR queues: documents awaiting verification and documents expiring soon."""
    _require_hr(user)
    company_id = company_id_of(user)
    q = _q(DocumentQueueQuery, query)
    today = _today()
    who = {'emp_code': F('employee__emp_code'), 'name_en': F('employee__name_en')}
    pending = _doc_values(_documents(company_id).filter(status=DocStatus.PENDING).order_by('created_at', 'id'), **who)
    expiring = _doc_values(
        _documents(company_id).filter(is_current=True, employee__status=ACTIVE, expiry_date__isnull=False,
                                      expiry_date__lte=today + timedelta(days=q['days']))
        .exclude(status=DocStatus.REJECTED).order_by('expiry_date', 'id'), **who)
    return {'pending': [_doc_row(r) for r in pending], 'expiring': [_doc_row(r, today) for r in expiring]}


def add_document(user: AuthUser, employee_id: str, body: dict) -> dict:
    company_id = company_id_of(user)
    eid = parse_uuid(employee_id)
    b = _body(DocumentIn, body)
    hr = is_hr(user)
    if not hr and user.employee_id != eid:
        raise ForbiddenError('you can only upload your own documents')
    emp = employee_row(company_id, eid)
    c = (DocumentCategory.objects.for_company(company_id).filter(pk=b['categoryId'])
         .values('name', 'employee_can_upload', 'expiry_required', 'visible_to_employee', 'active').first())
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
            o = (_documents(company_id).select_for_update().filter(pk=replaces)
                 .values('employee_id', 'version', 'is_current', 'uploaded_by_id', 'visible_to_employee').first())
            if (not o or str(o['employee_id']) != eid
                    or (not hr and not o['visible_to_employee'] and str(o['uploaded_by_id'] or '') != user.id)):
                raise NotFoundError('the document to replace was not found')
            if not o['is_current']:
                raise ConflictError('only the latest version can be replaced')
            version = o['version'] + 1
            EmployeeDocument.objects.filter(pk=replaces).update(is_current=False)
        d = EmployeeDocument.objects.create(
            company_id=company_id, employee_id=eid, category_id=b['categoryId'], title=b['title'], file_id=b['fileId'],
            version=version, replaces_id=replaces, status=DocStatus.VERIFIED if hr else DocStatus.PENDING,
            expiry_date=expiry, visible_to_employee=visible if hr else True, notes=b.get('notes'),
            uploaded_by_id=user.id, verified_by_id=user.id if hr else None, verified_at=clock_now() if hr else None)
        doc_id = str(d.id)
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
    """A document of the company (any employee); NotFoundError otherwise."""
    d = _doc_values(_documents(company_id).filter(pk=parse_uuid(doc_id))).first()
    if not d:
        raise NotFoundError('document not found')
    return _doc_row(d)


def document_for(user: AuthUser, doc_id: str) -> dict:
    """A document the user may see (HR: any in the company; the employee: their own visible ones and own uploads)."""
    d = load_doc(company_id_of(user), parse_uuid(doc_id))
    own = user.employee_id == d['employee_id'] and (d['visible_to_employee'] or d['uploaded_by'] == user.id)
    if not is_hr(user) and not own:
        raise NotFoundError('document not found')
    return d


def hr_document(user: AuthUser, doc_id: str) -> dict:
    """A document for HR's forms (edit, send back), with the employee's name."""
    _require_hr(user)
    company_id = company_id_of(user)
    d = load_doc(company_id, doc_id)
    d['employee_name'] = employee_row(company_id, d['employee_id'])['name_en']
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
    did = parse_uuid(doc_id)
    b = _body(DocumentVerifyIn, body)
    if b['decision'] == 'reject' and not b.get('reason'):
        raise BadRequestError('give a reason so the employee knows what to fix')
    d = load_doc(company_id, did)
    if user.employee_id == d['employee_id']:
        raise ForbiddenError('you cannot verify your own documents')
    verify = b['decision'] == 'verify'
    with transaction.atomic():
        n = (_documents(company_id).filter(pk=did, status=DocStatus.PENDING)
             .update(status=DocStatus.VERIFIED if verify else DocStatus.REJECTED,
                     reject_reason=None if verify else b['reason'], verified_by_id=user.id, verified_at=Now()))
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
    did = parse_uuid(doc_id)
    b = _body(DocumentPatchIn, body)
    before = load_doc(company_id, did)
    fields: dict = {}
    _set_if(fields, 'title', b.get('title'))
    _set_if(fields, 'visible_to_employee', b.get('visibleToEmployee'))
    if 'expiryDate' in b:
        fields['expiry_date'] = b['expiryDate']
    if 'notes' in b:
        fields['notes'] = b['notes']
    with transaction.atomic():
        if fields:
            _documents(company_id).filter(pk=did).update(**fields)
        # A new expiry date restarts its alerts.
        if 'expiryDate' in b and b['expiryDate'] != before['expiry_date']:
            DocumentExpiryAlert.objects.filter(document_id=did).delete()
        audit('update', 'employee_document', actor_id=user.id, row_id=did,
              before={'title': before['title'], 'expiryDate': before['expiry_date'], 'visible': before['visible_to_employee']},
              after=b)
    return {'ok': True}


def delete_document(user: AuthUser, doc_id: str) -> dict:
    """HR deletes any document; an employee only their own upload that HR has not verified yet. The bytes are wiped."""
    company_id = company_id_of(user)
    did = parse_uuid(doc_id)
    d = load_doc(company_id, did)
    own_pending = user.employee_id == d['employee_id'] and d['uploaded_by'] == user.id and d['status'] != DocStatus.VERIFIED
    if not is_hr(user) and not own_pending:
        if user.employee_id == d['employee_id']:
            raise ForbiddenError('only HR can delete a verified document')
        raise NotFoundError('document not found')
    with transaction.atomic():
        _documents(company_id).filter(pk=did).update(deleted_at=Now(), deleted_by_id=user.id, is_current=False)
        # Deleting the latest version brings the previous one back.
        if d['is_current'] and d['replaces_id']:
            _documents(company_id).filter(pk=d['replaces_id']).update(is_current=True)
        delete_file(company_id, d['file_id'])
        audit('delete', 'employee_document', actor_id=user.id, row_id=did,
              before={'employeeId': d['employee_id'], 'title': d['title'], 'version': d['version']})
    return {'ok': True}


# ---------------------------------------------------------------- letter settings and templates

TEMPLATE_FIELDS = ('id', 'code', 'name', 'subject', 'body', 'requestable', 'visible_to_employee', 'is_builtin', 'active',
                   'updated_at')


def placeholders(user: AuthUser) -> list[dict]:
    _require_hr(user)
    return PLACEHOLDERS


def get_letter_settings(user: AuthUser) -> dict:
    _require_hr(user)
    return letter_settings(company_id_of(user))


def save_letter_settings(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = _body(LetterSettingsIn, body)
    before = letter_settings(company_id)
    with transaction.atomic():
        put_setting(company_id, SETTINGS_KEY, b, updated_by=user.id)
        audit('update', 'company_setting', actor_id=user.id, row_id=SETTINGS_KEY, before=before, after=b)
    return b


def _templates(company_id: str) -> models.QuerySet:
    return LetterTemplate.objects.for_company(company_id)


def list_templates(user: AuthUser) -> list[dict]:
    """HR sees every template; employees only the ones they may request."""
    company_id = company_id_of(user)
    ensure_defaults(company_id)
    if is_hr(user):
        qs = _templates(company_id).order_by('-active', 'name').values(*TEMPLATE_FIELDS)
    else:
        qs = (_templates(company_id).filter(active=True, requestable=True).order_by('name')
              .values('id', 'code', 'name', 'subject'))
    return [plain(r) for r in qs]


def get_template(user: AuthUser, template_id: str) -> dict:
    _require_hr(user)
    t = _templates(company_id_of(user)).filter(pk=parse_uuid(template_id)).values(*TEMPLATE_FIELDS).first()
    if not t:
        raise NotFoundError('template not found')
    return plain(t)


def _check_placeholders(subject: str, body: str) -> None:
    bad = unknown_placeholders(subject, body)
    if bad:
        raise BadRequestError(f"Unknown placeholder{'' if len(bad) == 1 else 's'}: {', '.join(bad)}", {'unknown': bad})


def create_template(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = _body(LetterTemplateIn, body)
    _check_placeholders(b['subject'], b['body'])
    with transaction.atomic():
        t = LetterTemplate.objects.create(
            company_id=company_id, code=b['code'], name=b['name'], subject=b['subject'], body=b['body'],
            requestable=b['requestable'], visible_to_employee=b['visibleToEmployee'], updated_by_id=user.id)
        audit('create', 'letter_template', actor_id=user.id, row_id=t.id, after={'code': b['code'], 'name': b['name']})
    return {'id': str(t.id)}


_TEMPLATE_COLUMNS = {'name': 'name', 'subject': 'subject', 'body': 'body', 'requestable': 'requestable',
                     'visibleToEmployee': 'visible_to_employee', 'active': 'active'}


def update_template(user: AuthUser, template_id: str, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    tid = parse_uuid(template_id)
    b = _body(LetterTemplatePatchIn, body)
    _check_placeholders(b.get('subject') or '', b.get('body') or '')
    qs = _templates(company_id).filter(pk=tid)
    before = qs.values('name', 'subject', 'body', 'requestable', 'visible_to_employee', 'active').first()
    if not before:
        raise NotFoundError('template not found')
    fields: dict = {'updated_by_id': user.id, 'updated_at': Now()}
    for key, column in _TEMPLATE_COLUMNS.items():
        _set_if(fields, column, b.get(key))
    with transaction.atomic():
        qs.update(**fields)
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
    b = _body(LetterPreviewIn, body)
    if b.get('templateId'):
        t = _templates(company_id).filter(pk=b['templateId']).values('subject', 'body').first()
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

def _letters(company_id: str) -> models.QuerySet:
    return IssuedLetter.objects.for_company(company_id)


def _requests(company_id: str) -> models.QuerySet:
    return LetterRequest.objects.for_company(company_id)


def issue_letter(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = _body(LetterIssueIn, body)
    ensure_defaults(company_id)
    request_id = b.get('requestId')
    with transaction.atomic():
        tpl = (_templates(company_id).filter(pk=b['templateId'])
               .values('id', 'code', 'subject', 'body', 'active', 'visible_to_employee').first())
        if not tpl or not tpl['active']:
            raise NotFoundError('template not found')
        employee_row(company_id, b['employeeId'])
        purpose, addressed_to = b.get('purpose'), b.get('addressedTo')
        if request_id:
            q = (_requests(company_id).select_for_update().filter(pk=request_id)
                 .values('employee_id', 'status', 'purpose', 'addressed_to', 'workflow_instance_id').first())
            if not q:
                raise NotFoundError('letter request not found')
            if str(q['employee_id']) != b['employeeId']:
                raise BadRequestError('the request belongs to another employee')
            if q['status'] not in OPEN_REQUEST:
                raise ConflictError(f"request already {q['status']}")
            purpose = purpose if purpose is not None else q['purpose']
            addressed_to = addressed_to if addressed_to is not None else q['addressed_to']
            if q['status'] == ReqStatus.PENDING and q['workflow_instance_id']:
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
        row = IssuedLetter.objects.create(
            company_id=company_id, employee_id=b['employeeId'], template_id=tpl['id'], template_code=tpl['code'],
            ref_no=ref_no, subject=letter['subject'], html=letter['html'], visible_to_employee=visible,
            request_id=request_id, issued_by_id=user.id)
        letter_id = str(row.id)
        if request_id:
            _requests(company_id).filter(pk=request_id).update(
                status=ReqStatus.ISSUED, issued_letter_id=letter_id, decided_at=Coalesce('decided_at', Now()))
        audit('issue', 'issued_letter', actor_id=user.id, row_id=letter_id,
              after={'employeeId': b['employeeId'], 'template': tpl['code'], 'refNo': ref_no, 'requestId': request_id})
        if visible:
            notify(company_id=company_id, user_id=user_id_for_employee(b['employeeId']), employee_id=b['employeeId'],
                   template='letter.issued', title=f"{letter['subject']} issued",
                   body=f'HR issued your letter "{letter["subject"]}" ({ref_no}). Open it under Profile, Documents & letters.',
                   payload={'letterId': letter_id}, channels=('push',))
    return {'id': letter_id, 'refNo': ref_no}


LETTER_FIELDS = ('id', 'employee_id', 'template_code', 'ref_no', 'subject', 'visible_to_employee', 'request_id',
                 'issued_at', 'voided_at', 'void_reason')


def list_letters(user: AuthUser, query: dict | None = None) -> list[dict]:
    company_id = company_id_of(user)
    q = _q(LettersQuery, query)
    hr = is_hr(user)
    if not hr and q.get('employeeId') and q['employeeId'] != user.employee_id:
        raise ForbiddenError('you can only see your own letters')
    employee_id = q.get('employeeId') if hr else user.employee_id
    if not hr and not employee_id:
        return []
    qs = _letters(company_id)
    if not hr:
        qs = qs.filter(visible_to_employee=True, voided_at__isnull=True)
    qs = LetterFilter({'employeeId': employee_id, 'q': q.get('q')}, queryset=qs).qs
    qs = (qs.order_by('-issued_at', '-id')
          .values(*LETTER_FIELDS, emp_code=F('employee__emp_code'), name_en=F('employee__name_en'),
                  issued_by_name=F('issued_by__name'))[:q['limit']])
    return [plain(r) for r in qs]


def letter_ref(user: AuthUser, letter_id: str) -> dict:
    """{id, ref_no} of a letter of the company, for HR's void form."""
    _require_hr(user)
    r = _letters(company_id_of(user)).filter(pk=parse_uuid(letter_id)).values('id', 'ref_no').first()
    if not r:
        raise NotFoundError('letter not found')
    return plain(r)


def letter_html(user: AuthUser, letter_id: str) -> str:
    company_id = company_id_of(user)
    lid = parse_uuid(letter_id)
    row = (_letters(company_id).filter(pk=lid)
           .values('employee_id', 'html', 'visible_to_employee', 'voided_at', 'void_reason').first())
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
    lid = parse_uuid(letter_id)
    b = _body(LetterVoidIn, body)
    with transaction.atomic():
        n = (_letters(company_id).filter(pk=lid, voided_at__isnull=True)
             .update(voided_at=Now(), voided_by_id=user.id, void_reason=b['reason']))
        if not n:
            raise NotFoundError('letter not found or already voided')
        audit('void', 'issued_letter', actor_id=user.id, row_id=lid, after=b)
    return {'ok': True}


# ---------------------------------------------------------------- letter requests

REQUEST_FIELDS = ('id', 'employee_id', 'template_id', 'purpose', 'addressed_to', 'status')


def _request_values(qs: models.QuerySet, *extra) -> models.QuerySet:
    return qs.values(*REQUEST_FIELDS, *extra, emp_code=F('employee__emp_code'), name_en=F('employee__name_en'),
                     template_name=F('template__name'), template_code=F('template__code'))


def request_letter(user: AuthUser, body: dict) -> dict:
    company_id = company_id_of(user)
    if not user.employee_id:
        raise BadRequestError('only employees can request letters')
    employee_id = user.employee_id
    b = _body(LetterRequestIn, body)
    ensure_defaults(company_id)
    name = (_templates(company_id).filter(pk=b['templateId'], active=True, requestable=True)
            .values_list('name', flat=True).first())
    if not name:
        raise BadRequestError('this letter cannot be requested')
    if _requests(company_id).filter(employee_id=employee_id, template_id=b['templateId'], status__in=OPEN_REQUEST).exists():
        raise ConflictError(f'you already have an open request for a {name.lower()}')
    emp = employee_row(company_id, employee_id)
    with transaction.atomic():
        r = LetterRequest.objects.create(company_id=company_id, employee_id=employee_id, template_id=b['templateId'],
                                         purpose=b['purpose'], addressed_to=b.get('addressedTo'), created_by_id=user.id)
        req_id = str(r.id)
        inst = start_workflow(company_id=company_id, object_type='letter_request', object_id=req_id, employee_id=employee_id,
                              title=f'{name} requested', body=f"{emp['name_en']} asked for a {name.lower()}: {b['purpose']}")
        LetterRequest.objects.filter(pk=req_id).update(workflow_instance_id=inst['id'])
        audit('create', 'letter_request', actor_id=user.id, row_id=req_id, after=b)
    return {'id': req_id}


def list_requests(user: AuthUser, query: dict | None = None) -> list[dict]:
    company_id = company_id_of(user)
    q = _q(RequestsQuery, query)
    hr = is_hr(user)
    if not hr and not user.employee_id:
        return []
    filters = {'status': q.get('status')}
    if not hr:
        filters['employeeId'] = user.employee_id
    qs = LetterRequestFilter(filters, queryset=_requests(company_id)).qs.order_by('-created_at', '-id')
    return [plain(r) for r in _request_values(qs, 'decision_reason', 'issued_letter_id', 'created_at', 'decided_at')[:300]]


def get_request(user: AuthUser, request_id: str) -> dict:
    """One letter request, for HR preparing the letter."""
    _require_hr(user)
    r = _request_values(_requests(company_id_of(user)).filter(pk=parse_uuid(request_id))).first()
    if not r:
        raise NotFoundError('letter request not found')
    return plain(r)


def cancel_request(user: AuthUser, request_id: str) -> dict:
    company_id = company_id_of(user)
    rid = parse_uuid(request_id)
    row = _requests(company_id).filter(pk=rid).values('employee_id', 'status').first()
    if not row or (str(row['employee_id']) != user.employee_id and not is_hr(user)):
        raise NotFoundError('request not found')
    if row['status'] not in OPEN_REQUEST:
        raise ConflictError(f"request already {row['status']}")
    with transaction.atomic():
        _requests(company_id).filter(pk=rid).update(status=ReqStatus.CANCELLED, decided_at=Now())
        cancel_workflow('letter_request', rid)
        audit('cancel', 'letter_request', actor_id=user.id, row_id=rid)
    return {'ok': True}


# ---------------------------------------------------------------- company policies

POLICY_FIELDS = ('id', 'title', 'description', 'audience', 'audience_ids', 'requires_ack', 'published_at')


def _policies(company_id: str) -> models.QuerySet:
    return CompanyPolicy.objects.for_company(company_id)


def _my_ack(employee_id: str | None):
    """When this employee acknowledged the outer policy (NULL if not, or if there is no employee record)."""
    if not employee_id:
        return models.Value(None, output_field=models.DateTimeField())
    return Subquery(PolicyAck.objects.filter(policy=OuterRef('pk'), employee_id=employee_id).values('acknowledged_at')[:1])


def list_policies(user: AuthUser, query: dict | None = None) -> list[dict]:
    company_id = company_id_of(user)
    q = _q(PolicyArchivedQuery, query)
    if is_hr(user):
        qs = _policies(company_id)
        if q.get('archived') != '1':
            qs = qs.filter(archived_at__isnull=True)
        qs = (qs.order_by(F('archived_at').asc(nulls_first=True), '-published_at')
              .values(*POLICY_FIELDS, 'archived_at', filename=F('file__filename'), my_ack_at=_my_ack(user.employee_id),
                      acks=Count('policy_acks', filter=Q(policy_acks__employee__status=ACTIVE))))
        out = []
        audiences = AudienceIndex(company_id)
        for p in qs:
            p = plain(p)
            ids = audiences.ids(p)
            out.append({**p, 'audience_size': len(ids),
                        'in_audience': bool(user.employee_id) and not p['archived_at'] and user.employee_id in ids})
        return out
    if not user.employee_id:
        return []
    qs = (_policies(company_id).filter(archived_at__isnull=True).order_by('-published_at')
          .values(*POLICY_FIELDS, filename=F('file__filename'), my_ack_at=_my_ack(user.employee_id)))
    out = []
    audiences = AudienceIndex(company_id, user.employee_id)
    for p in qs:
        p = plain(p)
        if audiences.ids(p):
            out.append({'id': p['id'], 'title': p['title'], 'description': p['description'], 'requires_ack': p['requires_ack'],
                        'published_at': p['published_at'], 'filename': p['filename'], 'my_ack_at': p['my_ack_at'],
                        'in_audience': True})
    return out


def publish_policy(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    b = _body(CompanyPolicyIn, body)
    aud, aud_ids = b['audience'], b['audienceIds']
    if aud != Audience.ALL and not aud_ids:
        raise BadRequestError(f'choose at least one {aud}')
    if aud != Audience.ALL:
        model = Branch if aud == Audience.BRANCH else OrgUnit
        if model.objects.for_company(company_id).filter(pk__in=aud_ids).count() != len(set(aud_ids)):
            raise BadRequestError(f'unknown {aud}')
    stored_ids = [] if aud == Audience.ALL else aud_ids
    with transaction.atomic():
        p = CompanyPolicy.objects.create(company_id=company_id, title=b['title'], description=b.get('description'),
                                         file_id=b['fileId'], audience=aud, audience_ids=stored_ids,
                                         requires_ack=b['requiresAck'], created_by_id=user.id)
        pid = str(p.id)
        attach_file(company_id=company_id, file_id=b['fileId'], user_id=user.id, purpose='company_document',
                    attached_to=f'company_policy:{pid}')
        nudged = []
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
                nudged.append(PolicyNudge(policy_id=pid, employee_id=employee_id))
        PolicyNudge.objects.bulk_create(nudged, ignore_conflicts=True)
        audit('create', 'company_policy', actor_id=user.id, row_id=pid,
              after={'title': b['title'], 'audience': aud, 'requiresAck': b['requiresAck']})
    return {'id': pid}


def update_policy(user: AuthUser, policy_id: str, body: dict) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    pid = parse_uuid(policy_id)
    b = _body(CompanyPolicyPatchIn, body)
    fields: dict = {}
    _set_if(fields, 'title', b.get('title'))
    _set_if(fields, 'requires_ack', b.get('requiresAck'))
    if 'description' in b:
        fields['description'] = b['description']
    if b.get('archived') is not None:
        fields['archived_at'] = Coalesce('archived_at', Now()) if b['archived'] else None
    with transaction.atomic():
        qs = _policies(company_id).filter(pk=pid)
        if not (qs.update(**fields) if fields else qs.exists()):
            raise NotFoundError('policy not found')
        audit('update', 'company_policy', actor_id=user.id, row_id=pid, after=b)
    return {'ok': True}


def _load_policy_for(user: AuthUser, policy_id: str) -> tuple[str, dict, bool]:
    company_id = company_id_of(user)
    row = (_policies(company_id).filter(pk=parse_uuid(policy_id))
           .values('id', 'title', 'file_id', 'audience', 'audience_ids', 'requires_ack', 'archived_at').first())
    if not row:
        raise NotFoundError('policy not found')
    row = plain(row)
    member = bool(user.employee_id) and not row['archived_at'] and bool(audience_ids(company_id, row, user.employee_id))
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
    PolicyAck.objects.bulk_create([PolicyAck(policy_id=row['id'], employee_id=user.employee_id)], ignore_conflicts=True)
    return {'ok': True}


def policy_acks(user: AuthUser, policy_id: str) -> list[dict]:
    _require_hr(user)
    company_id, row, _ = _load_policy_for(user, policy_id)
    ids = audience_ids(company_id, row)
    acked = PolicyAck.objects.filter(policy_id=row['id'], employee=OuterRef('pk')).values('acknowledged_at')[:1]
    nudged = PolicyNudge.objects.filter(policy_id=row['id'], employee=OuterRef('pk')).values('last_sent_at')[:1]
    qs = (Employee.objects.for_company(company_id).filter(pk__in=ids)
          .annotate(acknowledged_at=Subquery(acked), last_reminded_at=Subquery(nudged))
          .order_by(F('acknowledged_at').asc(nulls_first=True), 'emp_code')
          .values('emp_code', 'name_en', 'acknowledged_at', 'last_reminded_at', employee_id=F('id')))
    return [plain(r) for r in qs]


def remind_policy(user: AuthUser, policy_id: str) -> dict:
    _require_hr(user)
    company_id, row, _ = _load_policy_for(user, policy_id)
    if row['archived_at']:
        raise ConflictError('this policy is archived')
    ids = audience_ids(company_id, row)
    done = _acknowledged(row['id'], ids)
    sent = sum(1 for employee_id in ids if employee_id not in done and _remind(company_id, row, employee_id))
    return {'sent': sent}


# ---------------------------------------------------------------- pickers for the pages

def branches(user: AuthUser) -> list[dict]:
    return [plain(r) for r in Branch.objects.for_company(company_id_of(user)).order_by('name_en').values('id', 'name_en')]


def org_units(user: AuthUser) -> list[dict]:
    return [plain(r) for r in OrgUnit.objects.for_company(company_id_of(user)).order_by('name_en').values('id', 'name_en')]
