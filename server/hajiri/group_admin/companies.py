"""
Companies of the group (/group/companies): list, add (seeded with the statutory defaults), edit names and the
company profile (PAN, address, logo), and attach an existing standalone company the actor administers.
Group admins only; every change is audited.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Count, OuterRef, Q

from hajiri.core import companies as core_companies
from hajiri.core import settings_store
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser
from hajiri.core.collation import collate_key
from hajiri.core.errors import ConflictError, NotFoundError
from hajiri.core.validation import plain, validate
from hajiri.db.models import Company, Employee

from .common import group_scope
from .serializers import AttachIn, CompanyIn, CompanyPatchIn

PROFILE_KEY = 'company.profile'
_PROFILE_FIELDS = ('nameNe', 'pan', 'address', 'logoDataUrl')


def _blank_to_none(b: dict) -> dict:
    return {k: (None if v == '' else v) for k, v in b.items()}


def list_companies(actor: AuthUser) -> list[dict]:
    """Every company of the group with its profile and head count."""
    scope = group_scope(actor)
    rows = (Company.objects.filter(pk__in=scope.ids)
            .annotate(headcount=Count('employees', filter=Q(employees__status=Employee.Status.ACTIVE)),
                      profile=settings_store.setting_subquery(PROFILE_KEY, company_ref=OuterRef('pk')))
            .values('id', 'code', 'name_en', 'name_ne', 'created_at', 'headcount', 'profile'))
    out = []
    for r in rows:
        p = r['profile'] or {}
        out.append({'id': str(r['id']), 'code': r['code'], 'nameEn': r['name_en'], 'nameNe': r['name_ne'],
                    'pan': p.get('pan'), 'address': p.get('address'), 'logoDataUrl': p.get('logoDataUrl'),
                    'headcount': r['headcount'], 'createdAt': r['created_at'], 'active': str(r['id']) == actor.company_id})
    out.sort(key=lambda r: collate_key(r['nameEn']))
    return out


def get_company(actor: AuthUser, company_id) -> dict:
    scope = group_scope(actor)
    c = scope.company(company_id)
    return next(r for r in list_companies(actor) if r['id'] == str(c.id))


def _save_profile(actor: AuthUser, company: Company, changes: dict) -> None:
    """Merge changes into the company's profile setting (names kept in step with the company row)."""
    before = settings_store.get_setting(company.id, PROFILE_KEY)
    profile = {**(before or {}), 'nameEn': company.name_en, 'nameNe': company.name_ne}
    for k in _PROFILE_FIELDS:
        if k in changes:
            profile[k] = changes[k]
    profile = {k: v for k, v in profile.items() if v is not None}
    if profile == before:
        return
    settings_store.put_setting(company.id, PROFILE_KEY, profile, updated_by=actor.id)
    audit('update', 'company_setting', actor_id=actor.id, row_id=f'{company.id}:{PROFILE_KEY}',
          before=_masked_logo(before), after=_masked_logo(profile))


def _masked_logo(p: dict | None) -> dict | None:
    """The audit keeps whether a logo was set, not the image itself."""
    if not p:
        return p
    return {**p, 'logoDataUrl': '(image)'} if p.get('logoDataUrl') else p


@transaction.atomic
def create_company(actor: AuthUser, body: dict) -> dict:
    """A new company of the actor's group. The foundation's create_group_company checks the code, seeds the
    statutory leave types, approval flows, settings and every module's defaults, and audits it; then the profile
    (PAN, address, logo) is stored. The admin of a company that is not in a group yet starts one this way, as on the
    Group page: the group is created around their company and they become its first group admin (the foundation
    checks the admin role)."""
    if actor.group_id:
        group_scope(actor)
    b = _blank_to_none(plain(validate(CompanyIn, body)))
    c = core_companies.create_group_company(actor, {'code': b['code'], 'name_en': b['nameEn'], 'name_ne': b.get('nameNe')})
    company = Company.objects.get(pk=c['id'])
    _save_profile(actor, company, {k: b.get(k) for k in _PROFILE_FIELDS})
    return {**c, 'nameEn': company.name_en}


@transaction.atomic
def update_company(actor: AuthUser, company_id, body: dict) -> dict:
    scope = group_scope(actor)
    company = Company.objects.select_for_update().get(pk=scope.company(company_id).pk)
    b = _blank_to_none(plain(validate(CompanyPatchIn, body)))
    before = {'nameEn': company.name_en, 'nameNe': company.name_ne}
    if 'nameEn' in b:
        company.name_en = b['nameEn'].strip()
    if 'nameNe' in b:
        company.name_ne = (b['nameNe'] or '').strip() or None
    after = {'nameEn': company.name_en, 'nameNe': company.name_ne}
    if after != before:
        company.save(update_fields=['name_en', 'name_ne'])
        audit('update', 'company', actor_id=actor.id, row_id=str(company.id), before=before, after=after)
    _save_profile(actor, company, {k: b[k] for k in _PROFILE_FIELDS if k in b})
    return get_company(actor, company.id)


def attachable_companies(actor: AuthUser) -> list[dict]:
    """Standalone companies (in no group) where the actor is admin: the only ones they may bring into the group."""
    group_scope(actor)
    ids = [c.id for c in actor.companies if c.group_id is None and c.has_role('admin')]
    rows = Company.objects.filter(pk__in=ids, group__isnull=True).values('id', 'code', 'name_en')
    return sorted(({'id': str(r['id']), 'code': r['code'], 'name': r['name_en']} for r in rows),
                  key=lambda r: collate_key(r['name']))


@transaction.atomic
def attach_company(actor: AuthUser, body: dict) -> dict:
    """Bring an existing company into the group. Only a company the actor administers (so nobody can pull in
    another firm's data); one already in a group stays there."""
    scope = group_scope(actor)
    cid = str(plain(validate(AttachIn, body))['companyId'])
    ref = actor.company(cid)
    if ref is None or not ref.has_role('admin'):
        raise NotFoundError('company not found')
    company = Company.objects.select_for_update().filter(pk=cid).first()
    if company is None:
        raise NotFoundError('company not found')
    if company.group_id is not None:
        if str(company.group_id) == scope.group_id:
            raise ConflictError('this company is already in the group')
        raise ConflictError('this company belongs to another group')
    company.group_id = scope.group_id
    company.save(update_fields=['group'])
    audit('update', 'company', actor_id=actor.id, row_id=cid, before={'groupId': None}, after={'groupId': scope.group_id})
    return {'id': cid, 'code': company.code, 'name': company.name_en, 'groupId': scope.group_id}


def company_choices(actor: AuthUser) -> list[tuple[str, str]]:
    """(id, name) of the group's companies, by name, for form selects."""
    return [(str(c.id), c.name_en) for c in group_scope(actor).sorted()]
