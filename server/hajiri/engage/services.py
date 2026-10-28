"""
Engagement: announcements, kudos, celebrations, surveys and polls, and the anonymous suggestion box (port of
apps/api/src/modules/engage/routes.ts and service.ts). Everyone in the company reads the feed; posting announcements
is for HR (anyone) and managers (their own team); surveys are run by HR.

Functions take the signed-in AuthUser and, for writes, a body in the TS camelCase shape, validated by the serializers
in serializers.py with the zod rules and messages. Results are dicts in the TS response shape. Everything is scoped to
the user's ACTIVE company; an id of another company is not found.
"""
from __future__ import annotations

import csv
import io
import re
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import (Case, Count, Exists, F, IntegerField, Min, OuterRef, Prefetch, Q, Subquery, TextField,
                              Value, When)
from django.db.models.functions import Coalesce, Now

from engines.bs_calendar import BS_MONTH_NAMES_EN, BsRangeError, ad_to_bs, bs_month_bounds
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.clock import NPT
from hajiri.core.clock import now as _now
from hajiri.core.clock import npt_today as _npt_today
from hajiri.core.collation import collate_key
from hajiri.core.crypto import field_cipher
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.scoping import as_uuid
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (Announcement, AnnouncementAck, Branch, Employee, EngageComment, EngagePref,
                              EngageReaction, EngageSetting, EngageValue, Kudos, KudosRecipient, OrgUnit, Suggestion,
                              Survey, SurveyAnswer, SurveyQuestion, SurveyResponse)

from . import serializers as ser
from .filters import SuggestionFilter
from .audience import (Placement, accounts_of, audience_members, audience_q,  # noqa: F401
                       audience_target_exists, is_hr, placement_of)
from .surveys import (check_answers, is_open, iso, members_of, notify_opened, question_issues, questions_of,
                      raise_issues, respondent_key, survey_results)

REACTIONS = ser.REACTIONS
TARGET_TYPES = ser.TARGET_TYPES
SUGGESTION_CATEGORIES = ser.SUGGESTION_CATEGORIES
SUGGESTION_STATUSES = ser.SUGGESTION_STATUSES

AudienceKind = Announcement.AudienceKind
Target = EngageReaction.TargetType


def npt_today() -> str:
    return _npt_today().isoformat()


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def _notify(**kw) -> None:
    from hajiri.notifications.service import notify
    notify(**kw)


def _feature_on(company_id: str, key: str) -> bool:
    from hajiri.features.service import feature_on
    return feature_on(company_id, key)


def _before(v: datetime | None) -> datetime:
    return v or _now() + timedelta(seconds=60)


def bs_month_of(ad: str) -> dict:
    """BS month that contains an AD date, as AD bounds."""
    b = ad_to_bs(ad)
    m = bs_month_bounds(b.year, b.month)
    return {'year': b.year, 'month': b.month, 'startAd': m['startAd'], 'endAd': m['endAd']}


def _nepal_days(start: str, end: str) -> Q:
    """created_at falls on a Nepal date from `start` to `end` (inclusive), as a range of instants."""
    lo = datetime.combine(date.fromisoformat(start), dtime(), NPT)
    hi = datetime.combine(date.fromisoformat(end) + timedelta(days=1), dtime(), NPT)
    return Q(created_at__gte=lo, created_at__lt=hi)


# ---------------------------------------------------------------- company values

DEFAULT_VALUES = [
    ('Ownership', 'Took responsibility and saw it through', '🎯'),
    ('Teamwork', 'Made the people around them better', '🤝'),
    ('Customer first', 'Went the extra mile for a customer', '💚'),
    ('Innovation', 'Found a better way to do it', '💡'),
    ('Integrity', 'Did the right thing, even when it was hard', '🛡️'),
]


def ensure_default_values(company_id: str) -> None:
    """Companies set up after the migration get the same starter values the first time anyone opens the kudos form."""
    if EngageValue.objects.for_company(company_id).exists():
        return
    EngageValue.objects.bulk_create(
        [EngageValue(company_id=company_id, name=name, description=description, emoji=emoji, sort_order=i + 1)
         for i, (name, description, emoji) in enumerate(DEFAULT_VALUES)], ignore_conflicts=True)


# ---------------------------------------------------------------- celebrations

def _bs_of(ad):
    try:
        b = ad_to_bs(ad)
        return b, f'{b.month}-{b.day}'
    except (BsRangeError, ValueError):
        return None, None


def _label_of(ad: str) -> str:
    b, _ = _bs_of(ad)
    return f'{b.day} {BS_MONTH_NAMES_EN[b.month - 1]}' if b else ad


def celebrations(company_id: str, today: str, days: int) -> list[dict]:
    """
    Birthdays (only people who chose to show theirs), work anniversaries and new joiners from today for `days` days.
    Birthdays and anniversaries follow the Bikram Sambat calendar: the same BS month and day each year.
    """
    emps = (Employee.objects.for_company(company_id).filter(status=Employee.Status.ACTIVE)
            .filter(Q(dob__isnull=False) | Q(join_date__isnull=False))
            .values('id', 'name_en', 'designation', 'dob', 'join_date', show_birthday=F('engage_pref__show_birthday')))
    t = date.fromisoformat(today)
    window: dict[str, tuple[str, int, int]] = {}
    for i in range(days):
        ad = (t + timedelta(days=i)).isoformat()
        b, md = _bs_of(ad)
        if b and md not in window:
            window[md] = (ad, i, b.year)
    out = []
    for e in emps:
        eid = str(e['id'])
        base = {'employeeId': eid, 'name': e['name_en'], 'designation': e['designation']}
        if e['dob'] and e['show_birthday']:
            b, md = _bs_of(e['dob'])
            hit = window.get(md) if b else None
            if hit:
                out.append({'kind': 'birthday', 'targetId': f'birthday:{eid}:{hit[2]}', **base, 'date': hit[0],
                            'bsLabel': _label_of(hit[0]), 'years': None, 'daysAway': hit[1]})
        if e['join_date']:
            b, md = _bs_of(e['join_date'])
            hit = window.get(md) if b else None
            if b and hit and hit[2] > b.year:
                out.append({'kind': 'anniversary', 'targetId': f'anniversary:{eid}:{hit[2]}', **base, 'date': hit[0],
                            'bsLabel': _label_of(hit[0]), 'years': hit[2] - b.year, 'daysAway': hit[1]})
            ago = (t - e['join_date']).days
            if 0 <= ago <= 14:
                jd = e['join_date'].isoformat()
                out.append({'kind': 'joined', 'targetId': f'joined:{eid}', **base, 'date': jd, 'bsLabel': _label_of(jd),
                            'years': None, 'daysAway': -ago})
    return sorted(out, key=lambda c: (c['daysAway'], collate_key(c['name'])))


_CELEBRATION_ID = re.compile(r'^(birthday|anniversary|joined):([0-9a-f-]{36})(?::(\d{4}))?$')


def celebration_target_ok(company_id: str, target_id: str) -> bool:
    """A celebration target id refers to an employee of the company (and, for birthdays, one who shows it). An id
    that is not a uuid is refused with 400 'invalid value format', as the database refused it in TS."""
    m = _CELEBRATION_ID.match(target_id)
    if not m:
        return False
    r = (Employee.objects.for_company(company_id).filter(pk=as_uuid(m[2]))
         .values(show=F('engage_pref__show_birthday')).first())
    if not r:
        return False
    return m[1] != 'birthday' or r['show'] is True


# ---------------------------------------------------------------- reactions and comments (shared)

def reaction_summary(company_id: str, user_id: str, type_: str, ids: list[str]) -> tuple[dict, dict, dict]:
    counts: dict[str, dict] = {}
    mine: dict[str, list] = {}
    comments: dict[str, int] = {}
    if not ids:
        return counts, mine, comments
    for x in (EngageReaction.objects.for_company(company_id).filter(target_type=type_, target_id__in=list(ids))
              .values('target_id', 'emoji')
              .annotate(n=Count('*'), mine=Count('user_id', filter=Q(user_id=user_id)), first=Min('created_at'))
              .order_by('first')):
        counts.setdefault(x['target_id'], {})[x['emoji']] = x['n']
        if x['mine']:
            mine.setdefault(x['target_id'], []).append(x['emoji'])
    for x in (EngageComment.objects.for_company(company_id)
              .filter(target_type=type_, target_id__in=list(ids), deleted_at__isnull=True)
              .values('target_id').annotate(n=Count('id')).order_by()):
        comments[x['target_id']] = x['n']
    return counts, mine, comments


def _visible_announcements(user: AuthUser, *, every: bool = False):
    """Announcements of the active company this user may see: HR (or `every`) all of them, others their own and the
    ones whose audience includes them."""
    qs = Announcement.objects.for_company(company_id_of(user)).filter(deleted_at__isnull=True)
    if every:
        return qs
    return qs.filter(Q(author_id=user.id) | audience_q(placement_of(user.employee_id), True))


def visible_announcement(user: AuthUser, id_: str) -> Announcement | None:
    return _visible_announcements(user, every=is_hr(user)).filter(pk=id_).first()


def can_see_target(user: AuthUser, type_: str, id_: str) -> bool:
    """May this user see (and so react to or comment on) the target?"""
    company = company_id_of(user)
    if type_ == Target.CELEBRATION:
        return celebration_target_ok(company, id_)
    if not re.fullmatch(r'[0-9a-f-]{36}', id_):
        return False
    pk = as_uuid(id_)
    if type_ == Target.KUDOS:
        return Kudos.objects.for_company(company).filter(pk=pk, deleted_at__isnull=True).exists()
    return visible_announcement(user, pk) is not None


def react(user: AuthUser, body: dict) -> dict:
    b = validate(ser.ReactIn, body)
    company = company_id_of(user)
    if not can_see_target(user, b['targetType'], b['targetId']):
        raise NotFoundError('not found')
    key = {'target_type': b['targetType'], 'target_id': b['targetId'], 'user_id': user.id, 'emoji': b['emoji']}
    with transaction.atomic():
        deleted, _ = EngageReaction.objects.for_company(company).filter(**key).delete()
        if not deleted:
            EngageReaction.objects.bulk_create([EngageReaction(company_id=company, **key)], ignore_conflicts=True)
    counts, mine, comments = reaction_summary(company, user.id, b['targetType'], [b['targetId']])
    return {'reacted': not deleted, 'reactions': counts.get(b['targetId'], {}), 'mine': mine.get(b['targetId'], []),
            'comments': comments.get(b['targetId'], 0)}


def list_comments(user: AuthUser, query: dict) -> list[dict]:
    q = validate(ser.TargetIn, query)
    company = company_id_of(user)
    if not can_see_target(user, q['targetType'], q['targetId']):
        raise NotFoundError('not found')
    out = (EngageComment.objects.for_company(company)
           .filter(target_type=q['targetType'], target_id=q['targetId'], deleted_at__isnull=True)
           .order_by('created_at', 'id')
           .values('id', 'body', 'created_at', 'user_id', name=Coalesce('user__employee__name_en', 'user__name')))[:200]
    hr = is_hr(user)
    return [{'id': str(x['id']), 'body': x['body'], 'createdAt': x['created_at'], 'name': x['name'],
             'canDelete': str(x['user_id']) == user.id or hr} for x in out]


def add_comment(user: AuthUser, body: dict) -> dict:
    b = validate(ser.CommentIn, body)
    company = company_id_of(user)
    if not can_see_target(user, b['targetType'], b['targetId']):
        raise NotFoundError('not found')
    with transaction.atomic():
        c = EngageComment.objects.create(company_id=company, target_type=b['targetType'], target_id=b['targetId'],
                                         user_id=user.id, body=b['body'])
        # Tell the people a kudos or celebration is about that someone wrote to them.
        recipients: list[str] = []
        if b['targetType'] == Target.KUDOS:
            people = KudosRecipient.objects.filter(kudos_id=b['targetId']).values_list('employee_id', flat=True)
            recipients = list(accounts_of(people).values())
            recipients += [str(g) for g in Kudos.objects.filter(pk=b['targetId']).values_list('giver_id', flat=True)]
        elif b['targetType'] == Target.CELEBRATION:
            recipients = list(accounts_of([b['targetId'].split(':')[1]]).values())
        for uid in dict.fromkeys(recipients):
            if uid == user.id:
                continue
            _notify(company_id=company, user_id=uid, template='engage.comment',
                    title='Someone sent you a wish' if b['targetType'] == Target.CELEBRATION else 'New comment on kudos',
                    body=f"{user.name}: {b['body'][:140]}", payload={'targetType': b['targetType'], 'targetId': b['targetId']})
    return {'id': str(c.id), 'targetType': b['targetType'], 'targetId': b['targetId']}


def delete_comment(user: AuthUser, id_: str) -> dict:
    id_ = parse_uuid(id_)
    c = EngageComment.objects.for_company(company_id_of(user)).filter(pk=id_, deleted_at__isnull=True).first()
    if not c:
        raise NotFoundError('comment not found')
    if str(c.user_id) != user.id and not is_hr(user):
        raise ForbiddenError('you can only remove your own comments')
    with transaction.atomic():
        EngageComment.objects.filter(pk=id_).update(deleted_at=Now())
        if str(c.user_id) != user.id:
            audit('delete', 'engage_comment', actor_id=user.id, row_id=id_)
    return {'ok': True, 'targetType': c.target_type, 'targetId': c.target_id}


# ---------------------------------------------------------------- announcements

def announcements_for(user: AuthUser, *, before: datetime | None = None, limit: int = 20, pinned_only: bool = False,
                      include_expired: bool = False) -> list[dict]:
    company = company_id_of(user)
    qs = _visible_announcements(user, every=include_expired).filter(created_at__lt=before or _before(None))
    if not include_expired:
        qs = qs.filter(Q(expires_on__isnull=True) | Q(expires_on__gte=_npt_today()))
    if pinned_only:
        qs = qs.filter(pinned=True)
    audience_name = Case(
        When(audience_kind=AudienceKind.BRANCH,
             then=Subquery(Branch.objects.filter(pk=OuterRef('audience_id')).values('name_en')[:1])),
        When(audience_kind=AudienceKind.DEPARTMENT,
             then=Subquery(OrgUnit.objects.filter(pk=OuterRef('audience_id')).values('name_en')[:1])),
        default=Value(None), output_field=TextField())
    out = list(qs.annotate(author_name=Coalesce('author_employee__name_en', 'author__name'),
                           acked=Exists(AnnouncementAck.objects.filter(announcement=OuterRef('pk'), user_id=user.id)),
                           audience_name=audience_name)
               .order_by('-created_at', '-id')[:limit])
    ids = [str(a.id) for a in out]
    counts, mine, comments = reaction_summary(company, user.id, Target.ANNOUNCEMENT, ids)
    hr = is_hr(user)
    items = []
    for a in out:
        aid = str(a.id)
        kind = a.audience_kind
        label = ('Everyone' if kind == AudienceKind.ALL else f"{a.author_name}'s team" if kind == AudienceKind.REPORTS
                 else a.audience_name or '—')
        items.append({
            'type': 'announcement', 'id': aid, 'createdAt': a.created_at, 'title': a.title, 'body': a.body,
            'authorName': a.author_name, 'audienceKind': kind, 'audienceId': _s(a.audience_id), 'audienceLabel': label,
            'pinned': a.pinned, 'requiresAck': a.requires_ack, 'acked': a.acked, 'expiresOn': iso(a.expires_on),
            'popup': a.popup, 'popupUntil': iso(a.popup_until),
            'reactions': counts.get(aid, {}), 'mine': mine.get(aid, []), 'comments': comments.get(aid, 0),
            'canEdit': str(a.author_id) == user.id or hr,
        })
    return items


def list_announcements(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = validate(ser.AnnouncementQuery, query or {})
    # HR may list every announcement (including expired ones and other teams') to manage them.
    every = q.get('all') == '1' and is_hr(user)
    return announcements_for(user, before=_before(q.get('before')), limit=q['limit'], include_expired=every)


def post_announcement(user: AuthUser, body: dict) -> dict:
    b = plain(validate(ser.AnnouncementIn, body))
    company = company_id_of(user)
    hr = is_hr(user)
    if not has_role(user, 'hr', 'manager'):
        raise ForbiddenError('only HR and managers post announcements')
    if not hr and b['audienceKind'] != AudienceKind.REPORTS:
        raise ForbiddenError('managers post announcements to their own team')
    if b['audienceKind'] == AudienceKind.REPORTS and not user.employee_id:
        raise BadRequestError('this account has no team')
    if not audience_target_exists(company, b['audienceKind'], b.get('audienceId')):
        raise BadRequestError('pick the branch or department this is for')
    if b.get('expiresOn') and b['expiresOn'] < npt_today():
        raise BadRequestError('the end date has already passed')
    audience_id = None if b['audienceKind'] in (AudienceKind.ALL, AudienceKind.REPORTS) else b.get('audienceId')
    popup = b['popup'] and hr
    with transaction.atomic():
        a = Announcement.objects.create(
            company_id=company, author_id=user.id, author_employee_id=user.employee_id, title=b['title'], body=b['body'],
            audience_kind=b['audienceKind'], audience_id=audience_id, pinned=b['pinned'] and hr,
            requires_ack=b['requiresAck'], expires_on=b.get('expiresOn'), popup=popup,
            popup_until=b.get('popupUntil') if popup else None)
        aid = str(a.id)
        notified = 0
        for m in audience_members(company, b['audienceKind'], audience_id, user.employee_id):
            if not m['user_id'] or m['user_id'] == user.id:
                continue
            _notify(company_id=company, user_id=m['user_id'], employee_id=m['employee_id'], template='engage.announcement',
                    title=f"Please read: {b['title']}" if b['requiresAck'] else b['title'], body=b['body'][:200],
                    payload={'announcementId': aid})
            notified += 1
        audit('create', 'announcement', actor_id=user.id, row_id=aid, after={'title': b['title'], 'audience': b['audienceKind']})
    return {'id': aid, 'notified': notified}


def edit_announcement(user: AuthUser, id_: str, body: dict) -> dict:
    id_ = parse_uuid(id_)
    b = validate(ser.AnnouncementPatchIn, body)
    a = visible_announcement(user, id_)
    if not a:
        raise NotFoundError('announcement not found')
    hr = is_hr(user)
    if str(a.author_id) != user.id and not hr:
        raise ForbiddenError('only the author or HR can edit this')
    changes: dict[str, Any] = {'updated_at': Now()}
    for key, column in (('title', 'title'), ('body', 'body'), ('requiresAck', 'requires_ack'), ('expiresOn', 'expires_on')):
        if key in b:
            changes[column] = b[key]
    # Pinning and pop-ups are HR's: a manager's true is stored as false.
    if 'pinned' in b:
        changes['pinned'] = b['pinned'] and hr
    if 'popup' in b:
        changes['popup'] = b['popup'] and hr
    if 'popupUntil' in b and hr:
        changes['popup_until'] = b['popupUntil']
    with transaction.atomic():
        Announcement.objects.filter(pk=id_).update(**changes)
        audit('update', 'announcement', actor_id=user.id, row_id=id_, before={'title': a.title}, after=b)
    return {'ok': True}


def delete_announcement(user: AuthUser, id_: str) -> dict:
    id_ = parse_uuid(id_)
    a = visible_announcement(user, id_)
    if not a:
        raise NotFoundError('announcement not found')
    if str(a.author_id) != user.id and not is_hr(user):
        raise ForbiddenError('only the author or HR can remove this')
    with transaction.atomic():
        Announcement.objects.filter(pk=id_).update(deleted_at=Now())
        audit('delete', 'announcement', actor_id=user.id, row_id=id_, before={'title': a.title})
    return {'ok': True}


def ack_announcement(user: AuthUser, id_: str) -> dict:
    id_ = parse_uuid(id_)
    a = visible_announcement(user, id_)
    if not a:
        raise NotFoundError('announcement not found')
    if not a.requires_ack:
        raise BadRequestError('this announcement does not ask for confirmation')
    AnnouncementAck.objects.bulk_create([AnnouncementAck(announcement_id=id_, user_id=user.id)], ignore_conflicts=True)
    return {'ok': True}


def announcement_acks(user: AuthUser, id_: str) -> dict:
    """Who has and has not confirmed reading, for the author and HR."""
    id_ = parse_uuid(id_)
    company = company_id_of(user)
    a = visible_announcement(user, id_)
    if not a:
        raise NotFoundError('announcement not found')
    if str(a.author_id) != user.id and not is_hr(user):
        raise ForbiddenError('only the author or HR can see confirmations')
    members = audience_members(company, a.audience_kind, _s(a.audience_id), _s(a.author_employee_id))
    ack_by = {str(u): at for u, at in AnnouncementAck.objects.filter(announcement_id=id_).values_list('user_id', 'acked_at')}
    names = {str(n['id']): n for n in Employee.objects.for_company(company)
             .filter(pk__in=[m['employee_id'] for m in members]).values('id', 'name_en', 'emp_code')}
    author = str(a.author_id)
    people = [{'employeeId': m['employee_id'], 'name': (names.get(m['employee_id']) or {}).get('name_en') or '',
               'empCode': (names.get(m['employee_id']) or {}).get('emp_code') or '', 'ackedAt': ack_by.get(m['user_id'])}
              for m in members if m['user_id'] and m['user_id'] != author]
    people.sort(key=lambda p: (p['ackedAt'] is not None, collate_key(p['name'])))
    return {'audience': len(people), 'acked': sum(1 for p in people if p['ackedAt']), 'people': people,
            'title': a.title}


# ---------------------------------------------------------------- company values and settings

def _kudos_limit(company_id: str) -> int | None:
    return EngageSetting.objects.for_company(company_id).values_list('kudos_monthly_limit', flat=True).first()


def list_values(user: AuthUser) -> dict:
    company = company_id_of(user)
    ensure_default_values(company)
    vals = list(EngageValue.objects.for_company(company).order_by('sort_order', 'name')
                .values('id', 'name', 'description', 'emoji', 'active', sortOrder=F('sort_order')))
    for v in vals:
        v['id'] = str(v['id'])
    return {'values': vals, 'kudosMonthlyLimit': _kudos_limit(company)}


def _require_hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


def create_value(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    b = validate(ser.ValueIn, body)
    company = company_id_of(user)
    with transaction.atomic():
        v = EngageValue.objects.create(company_id=company, name=b['name'], description=b.get('description'),
                                       emoji=b['emoji'], active=b['active'], sort_order=b['sortOrder'])
        audit('create', 'engage_value', actor_id=user.id, row_id=str(v.id), after=b)
    return {'id': str(v.id)}


def update_value(user: AuthUser, id_: str, body: dict) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    b = validate(ser.ValuePatchIn, body)
    qs = EngageValue.objects.for_company(company_id_of(user)).filter(pk=id_)
    changes = {column: b[key] for key, column in (('name', 'name'), ('description', 'description'), ('emoji', 'emoji'),
                                                  ('active', 'active'), ('sortOrder', 'sort_order')) if key in b}
    with transaction.atomic():
        if not (qs.update(**changes) if changes else qs.exists()):
            raise NotFoundError('value not found')
        audit('update', 'engage_value', actor_id=user.id, row_id=id_, after=b)
    return {'ok': True}


def save_settings(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    b = validate(ser.SettingsIn, body)
    company = company_id_of(user)
    with transaction.atomic():
        EngageSetting.objects.bulk_create(
            [EngageSetting(company_id=company, kudos_monthly_limit=b['kudosMonthlyLimit'])],
            update_conflicts=True, unique_fields=['company'], update_fields=['kudos_monthly_limit', 'updated_at'])
        audit('update', 'engage_setting', actor_id=user.id, row_id=company, after=b)
    return {'ok': True}


# ---------------------------------------------------------------- kudos

def colleagues(user: AuthUser) -> list[dict]:
    """Colleagues to pick from when giving kudos: name, code and designation only."""
    out = list(Employee.objects.for_company(company_id_of(user)).filter(status=Employee.Status.ACTIVE)
               .order_by('name_en').values('id', 'designation', name=F('name_en'), empCode=F('emp_code')))
    for c in out:
        c['id'] = str(c['id'])
    return out


def kudos_items(user: AuthUser, *, before: datetime, limit: int, employee_id: str | None = None) -> list[dict]:
    company = company_id_of(user)
    qs = Kudos.objects.for_company(company).filter(deleted_at__isnull=True, created_at__lt=before)
    if employee_id:
        qs = qs.filter(Q(giver_employee_id=employee_id)
                       | Exists(KudosRecipient.objects.filter(kudos=OuterRef('pk'), employee_id=employee_id)))
    out = list(qs.select_related('giver_employee', 'value')
               .prefetch_related(Prefetch('kudos_recipients', to_attr='people',
                                          queryset=KudosRecipient.objects.select_related('employee').order_by('employee__name_en')))
               .order_by('-created_at', '-id')[:limit])
    ids = [str(k.id) for k in out]
    counts, mine, comments = reaction_summary(company, user.id, Target.KUDOS, ids)
    hr = is_hr(user)
    return [{
        'type': 'kudos', 'id': str(k.id), 'createdAt': k.created_at, 'message': k.message,
        'giver': {'employeeId': str(k.giver_employee_id), 'name': k.giver_employee.name_en,
                  'designation': k.giver_employee.designation},
        'recipients': [{'employeeId': str(r.employee_id), 'name': r.employee.name_en, 'designation': r.employee.designation}
                       for r in k.people],
        'value': {'id': str(k.value_id), 'name': k.value.name, 'emoji': k.value.emoji} if k.value_id else None,
        'reactions': counts.get(str(k.id), {}), 'mine': mine.get(str(k.id), []), 'comments': comments.get(str(k.id), 0),
        'canDelete': str(k.giver_id) == user.id or hr,
    } for k in out]


def list_kudos(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = plain(validate(ser.KudosQuery, query or {}))
    return kudos_items(user, before=_before(q.get('before')), limit=q['limit'], employee_id=q.get('employeeId'))


def give_kudos(user: AuthUser, body: dict) -> dict:
    b = plain(validate(ser.KudosIn, body))
    company = company_id_of(user)
    giver = user.employee_id
    if not giver:
        raise ForbiddenError('only employees can give kudos')
    recipients = list(dict.fromkeys(b['recipientIds']))
    if giver in recipients:
        raise BadRequestError('You cannot give kudos to yourself.')
    found = Employee.objects.for_company(company).filter(pk__in=recipients, status=Employee.Status.ACTIVE).count()
    if found != len(recipients):
        raise BadRequestError('Pick colleagues from your company.')
    if b.get('valueId') and not EngageValue.objects.for_company(company).filter(pk=b['valueId'], active=True).exists():
        raise BadRequestError('Pick one of the company values.')
    with transaction.atomic():
        # Serialise per giver (a row lock on their employee record) so two quick clicks cannot both pass the limit.
        Employee.objects.select_for_update(no_key=True).filter(pk=giver).first()
        limit = _kudos_limit(company)
        if limit:
            m = bs_month_of(npt_today())
            used = (Kudos.objects.filter(giver_employee_id=giver, deleted_at__isnull=True)
                    .filter(_nepal_days(m['startAd'], m['endAd'])).count())
            if used >= limit:
                raise ConflictError(f'You have given all {limit} kudos for this month. You can give more next month.')
        k = Kudos.objects.create(company_id=company, giver_id=user.id, giver_employee_id=giver, value_id=b.get('valueId'),
                                 message=b['message'])
        kid = str(k.id)
        KudosRecipient.objects.bulk_create([KudosRecipient(kudos_id=kid, employee_id=e) for e in recipients])
        for e, uid in accounts_of(recipients).items():
            _notify(company_id=company, user_id=uid, employee_id=e, template='engage.kudos',
                    title=f'{user.name} gave you kudos', body=b['message'][:200], payload={'kudosId': kid}, channels=('push',))
    return {'id': kid}


def delete_kudos(user: AuthUser, id_: str) -> dict:
    id_ = parse_uuid(id_)
    k = Kudos.objects.for_company(company_id_of(user)).filter(pk=id_, deleted_at__isnull=True).only('giver_id').first()
    if not k:
        raise NotFoundError('kudos not found')
    if str(k.giver_id) != user.id and not is_hr(user):
        raise ForbiddenError('only the giver or HR can remove kudos')
    with transaction.atomic():
        Kudos.objects.filter(pk=id_).update(deleted_at=Now())
        audit('delete', 'kudos', actor_id=user.id, row_id=id_)
    return {'ok': True}


def leaderboard(user: AuthUser, query: dict | None = None) -> dict:
    """Most appreciated people, most generous givers and value counts for a BS month (default: this month)."""
    q = validate(ser.LeaderboardQuery, query or {})
    company = company_id_of(user)
    cur = bs_month_of(npt_today())
    year = q.get('bsYear', cur['year'])
    month = q.get('bsMonth', cur['month'])
    try:
        bounds = bs_month_bounds(year, month)
    except (BsRangeError, ValueError, KeyError):
        raise BadRequestError('that month is outside the supported calendar')
    in_month = Kudos.objects.for_company(company).filter(deleted_at__isnull=True).filter(
        _nepal_days(bounds['startAd'], bounds['endAd']))

    def people(qs, prefix):
        return [{'employeeId': str(x['eid']), 'name': x['name'], 'designation': x['designation'], 'count': x['count']}
                for x in qs.values(eid=F(f'{prefix}_id'), name=F(f'{prefix}__name_en'),
                                   designation=F(f'{prefix}__designation'))
                .annotate(count=Count('*')).order_by('-count', 'name')[:10]]
    received = people(KudosRecipient.objects.filter(kudos__in=in_month), 'employee')
    given = people(in_month, 'giver_employee')
    month_q = Q(kudoses__in=in_month)
    values = list(EngageValue.objects.for_company(company).annotate(count=Count('kudoses', filter=month_q))
                  .order_by('-count', 'sort_order').values('id', 'name', 'emoji', 'count'))
    for v in values:
        v['id'] = str(v['id'])
    return {'bsYear': year, 'bsMonth': month, 'total': in_month.count(), 'received': received, 'given': given,
            'values': values}


# ---------------------------------------------------------------- feed, celebrations, preferences, home card

def iso_instant(d: datetime) -> str:
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def feed(user: AuthUser, query: dict | None = None) -> dict:
    q = validate(ser.FeedQuery, query or {})
    company = company_id_of(user)
    before = _before(q.get('before'))
    limit = q['limit']
    kudos = kudos_items(user, before=before, limit=limit) if _feature_on(company, 'kudos') else []
    anns = announcements_for(user, before=before, limit=limit)
    items = sorted(kudos + anns, key=lambda i: i['createdAt'], reverse=True)[:limit]
    return {'items': items, 'nextBefore': iso_instant(items[-1]['createdAt']) if len(items) == limit else None}


def celebrations_for(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = validate(ser.CelebrationsQuery, query or {})
    company = company_id_of(user)
    out = celebrations(company, npt_today(), q['days'])
    counts, mine, comments = reaction_summary(company, user.id, Target.CELEBRATION, [c['targetId'] for c in out])
    return [{**c, 'reactions': counts.get(c['targetId'], {}), 'mine': mine.get(c['targetId'], []),
             'comments': comments.get(c['targetId'], 0), 'isMe': c['employeeId'] == user.employee_id} for c in out]


def get_prefs(user: AuthUser) -> dict:
    if not user.employee_id:
        return {'showBirthday': False, 'hasBirthday': False}
    p = (Employee.objects.for_company(user.company_id).filter(pk=user.employee_id)
         .values('dob', show=Coalesce('engage_pref__show_birthday', False)).first())
    return {'showBirthday': bool(p and p['show']), 'hasBirthday': bool(p and p['dob'])}


def save_prefs(user: AuthUser, body: dict) -> dict:
    b = validate(ser.PrefsIn, body)
    if not user.employee_id:
        raise ForbiddenError('only employees have celebration settings')
    EngagePref.objects.bulk_create([EngagePref(employee_id=user.employee_id, show_birthday=b['showBirthday'])],
                                   update_conflicts=True, unique_fields=['employee'],
                                   update_fields=['show_birthday', 'updated_at'])
    return {'ok': True}


def home(user: AuthUser) -> dict:
    """The Company card on Home: the latest pinned announcement, today's celebrations and the first survey waiting for
    an answer."""
    company = company_id_of(user)
    pinned = announcements_for(user, limit=1, pinned_only=True)
    todays = celebrations(company, npt_today(), 1)
    surveys = open_surveys_for(user) if _feature_on(company, 'surveys') else []
    waiting = [s for s in surveys if not s['answered']]
    return {
        'announcement': pinned[0] if pinned else None,
        'celebrations': [{'kind': c['kind'], 'name': c['name'], 'employeeId': c['employeeId'], 'years': c['years'],
                          'targetId': c['targetId']} for c in todays if c['daysAway'] == 0],
        'survey': ({'id': waiting[0]['id'], 'title': waiting[0]['title'], 'kind': waiting[0]['kind'],
                    'closesOn': waiting[0]['closesOn']} if waiting else None),
        'surveysWaiting': len(waiting),
    }


# ---------------------------------------------------------------- surveys and polls

def public_survey(s: Survey) -> dict:
    out = {
        'id': str(s.id), 'kind': s.kind, 'title': s.title, 'description': s.description, 'anonymous': s.anonymous,
        'minGroup': s.min_group, 'audienceKind': s.audience_kind, 'audienceId': _s(s.audience_id),
        'status': s.status, 'opensOn': iso(s.opens_on), 'closesOn': iso(s.closes_on), 'recurrence': s.recurrence,
        'seriesId': _s(s.series_id), 'createdAt': s.created_at, 'publishedAt': s.published_at, 'closedAt': s.closed_at,
    }
    if hasattr(s, 'responses'):
        out['responses'] = s.responses
    if hasattr(s, 'question_count'):
        out['questionCount'] = s.question_count
    return out


def survey_in_company(id_: str, company_id: str) -> Survey:
    s = Survey.objects.for_company(company_id).filter(pk=id_).first()
    if not s:
        raise NotFoundError('survey not found')
    return s


def in_audience(user: AuthUser, s: Survey) -> bool:
    """The user is an active employee of the survey's company whom its audience reaches."""
    if not user.employee_id:
        return False
    works_there = Employee.objects.filter(pk=user.employee_id, status=Employee.Status.ACTIVE, company=OuterRef('company'))
    return (Survey.objects.filter(pk=s.pk).filter(audience_q(placement_of(user.employee_id), False))
            .filter(Exists(works_there)).exists())


def has_answered(survey_id: str, user_id: str) -> bool:
    return SurveyResponse.objects.filter(
        survey_id=survey_id, respondent_key=respondent_key(field_cipher(), str(survey_id), user_id)).exists()


def open_surveys_for(user: AuthUser) -> list[dict]:
    """Surveys and polls open to me now, with whether I have answered."""
    if not user.employee_id:
        return []
    today = _npt_today()
    out = list(Survey.objects.for_company(company_id_of(user))
               .filter(status=Survey.Status.PUBLISHED, opens_on__lte=today, closes_on__gte=today)
               .filter(audience_q(placement_of(user.employee_id), False))
               .annotate(question_count=Count('survey_questions'))
               .order_by('closes_on', 'created_at'))
    cipher = field_cipher()
    keys = [respondent_key(cipher, str(s.id), user.id) for s in out]
    answered = {str(i) for i in SurveyResponse.objects.filter(respondent_key__in=keys).values_list('survey_id', flat=True)}
    return [{'id': str(s.id), 'kind': s.kind, 'title': s.title, 'description': s.description,
             'anonymous': s.anonymous, 'opensOn': iso(s.opens_on), 'closesOn': iso(s.closes_on),
             'questionCount': s.question_count, 'answered': str(s.id) in answered} for s in out]


def _count_of(model) -> Coalesce:
    """Rows of `model` (survey responses or questions) belonging to the outer survey."""
    n = model.objects.filter(survey=OuterRef('pk')).order_by().values('survey').annotate(n=Count('*')).values('n')
    return Coalesce(Subquery(n, output_field=IntegerField()), 0)


def manage_surveys(user: AuthUser) -> list[dict]:
    _require_hr(user)
    # Two counted subqueries rather than two joined Counts: joining both tables multiplies responses by questions.
    out = (Survey.objects.for_company(company_id_of(user))
           .annotate(responses=_count_of(SurveyResponse), question_count=_count_of(SurveyQuestion))
           .order_by('-created_at')[:200])
    return [public_survey(s) for s in out]


def get_survey(user: AuthUser, id_: str) -> dict:
    """One survey with its questions: HR any time, others only while it is open to them."""
    id_ = parse_uuid(id_)
    s = survey_in_company(id_, company_id_of(user))
    if not is_hr(user) and not (is_open(s, npt_today()) and in_audience(user, s)):
        raise NotFoundError('survey not found')
    return {'survey': public_survey(s), 'answered': has_answered(id_, user.id),
            'questions': [{'id': q['id'], 'kind': q['kind'], 'prompt': q['prompt'], 'options': q['options'],
                           'required': q['required']} for q in questions_of(id_)]}


def parse_survey_body(body: Any) -> dict:
    """surveyBody with its superRefine: choice questions need two different options, closing on or after opening,
    a poll has exactly one choice question."""
    b = plain(validate(ser.SurveyIn, body))
    b['questions'] = [dict(q) for q in b['questions']]
    issues: list = []
    for i, q in enumerate(b['questions']):
        issues += question_issues(q, ['questions', i])
    if b['closesOn'] < b['opensOn']:
        issues.append((['closesOn'], 'the closing day must be on or after the opening day'))
    if b['kind'] == Survey.Kind.POLL and (len(b['questions']) != 1 or b['questions'][0]['kind'] not in ('single', 'multi')):
        issues.append((['questions'], 'a poll has exactly one choice question'))
    raise_issues(issues)
    return b


def _write_questions(survey_id: str, questions: list[dict]) -> None:
    SurveyQuestion.objects.bulk_create([
        SurveyQuestion(survey_id=survey_id, position=i + 1, kind=q['kind'], prompt=q['prompt'], required=q['required'],
                       options=list(q['options']) if q['kind'] in ('single', 'multi') else [])
        for i, q in enumerate(questions)])


def _survey_fields(b: dict) -> dict:
    return {'kind': b['kind'], 'title': b['title'], 'description': b.get('description'), 'anonymous': b['anonymous'],
            'min_group': b['minGroup'], 'audience_kind': b['audienceKind'],
            'audience_id': None if b['audienceKind'] == Survey.AudienceKind.ALL else b.get('audienceId'),
            'opens_on': b['opensOn'], 'closes_on': b['closesOn'], 'recurrence': b['recurrence']}


def create_survey(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    b = parse_survey_body(body)
    company = company_id_of(user)
    if not audience_target_exists(company, b['audienceKind'], b.get('audienceId')):
        raise BadRequestError('pick the branch or department this is for')
    with transaction.atomic():
        s = Survey.objects.create(company_id=company, created_by_id=user.id, **_survey_fields(b))
        sid = str(s.id)
        _write_questions(sid, b['questions'])
        audit('create', 'survey', actor_id=user.id, row_id=sid,
              after={'title': b['title'], 'kind': b['kind'], 'anonymous': b['anonymous']})
    return {'id': sid}


def update_survey(user: AuthUser, id_: str, body: dict) -> dict:
    """Drafts can be changed freely; once published only the closing day may move (questions and anonymity are fixed)."""
    _require_hr(user)
    id_ = parse_uuid(id_)
    company = company_id_of(user)
    s = survey_in_company(id_, company)
    if s.status == Survey.Status.CLOSED:
        raise ConflictError('this survey is closed')
    if s.status == Survey.Status.PUBLISHED:
        b = validate(ser.SurveyExtendIn, body)
        if b['closesOn'] < iso(s.opens_on) or b['closesOn'] < npt_today():
            raise BadRequestError('the new closing day must be today or later')
        with transaction.atomic():
            Survey.objects.filter(pk=id_).update(closes_on=b['closesOn'], reminded_at=None)
            audit('update', 'survey', actor_id=user.id, row_id=id_, after=b)
        return {'ok': True}
    b = parse_survey_body(body)
    if not audience_target_exists(company, b['audienceKind'], b.get('audienceId')):
        raise BadRequestError('pick the branch or department this is for')
    with transaction.atomic():
        Survey.objects.filter(pk=id_).update(**_survey_fields(b))
        SurveyQuestion.objects.filter(survey_id=id_).delete()
        _write_questions(id_, b['questions'])
        audit('update', 'survey', actor_id=user.id, row_id=id_, after={'title': b['title']})
    return {'ok': True}


def delete_survey(user: AuthUser, id_: str) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    s = survey_in_company(id_, company_id_of(user))
    if s.status != Survey.Status.DRAFT:
        raise ConflictError('only drafts can be deleted; close a published survey instead')
    with transaction.atomic():
        SurveyQuestion.objects.filter(survey_id=id_).delete()
        Survey.objects.filter(pk=id_).delete()
        audit('delete', 'survey', actor_id=user.id, row_id=id_, before={'title': s.title})
    return {'ok': True}


def publish_survey(user: AuthUser, id_: str) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    s = survey_in_company(id_, company_id_of(user))
    if s.status != Survey.Status.DRAFT:
        raise ConflictError(f'this survey is already {s.status}')
    if iso(s.closes_on) < npt_today():
        raise BadRequestError('the closing day has passed; change the dates first')
    with transaction.atomic():
        if not Survey.objects.filter(pk=id_, status=Survey.Status.DRAFT).update(status=Survey.Status.PUBLISHED,
                                                                                 published_at=Now()):
            raise ConflictError('this survey was just published')
        s.refresh_from_db()
        notified = notify_opened(s) if iso(s.opens_on) <= npt_today() else 0
        audit('publish', 'survey', actor_id=user.id, row_id=id_)
    return {'notified': notified}


def close_survey(user: AuthUser, id_: str) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    s = survey_in_company(id_, company_id_of(user))
    if s.status != Survey.Status.PUBLISHED:
        raise ConflictError('only an open survey can be closed')
    with transaction.atomic():
        # Closing by hand ends the series too: a recurring pulse stops here.
        Survey.objects.filter(pk=id_).update(status=Survey.Status.CLOSED, closed_at=Now(),
                                             recurrence=Survey.Recurrence.NONE)
        audit('close', 'survey', actor_id=user.id, row_id=id_)
    return {'ok': True}


def _audience_size(s: Survey) -> int:
    return len(members_of(s))


def respond(user: AuthUser, id_: str, body: dict) -> dict:
    id_ = parse_uuid(id_)
    b = plain(validate(ser.RespondIn, body))
    company = company_id_of(user)
    s = survey_in_company(id_, company)
    if not is_open(s, npt_today()) or not in_audience(user, s):
        raise NotFoundError('this survey is not open to you')
    answer_rows = check_answers(questions_of(id_), [dict(a) for a in b['answers']])
    p = placement_of(user.employee_id)
    with transaction.atomic():
        try:
            with transaction.atomic():
                r = SurveyResponse.objects.create(
                    survey_id=id_, respondent_key=respondent_key(field_cipher(), id_, user.id),
                    employee_id=None if s.anonymous else user.employee_id, org_unit_id=p.org_unit_id,
                    branch_id=p.branch_id, submitted_on=npt_today())
        except IntegrityError as e:
            if getattr(e.__cause__, 'sqlstate', None) != '23505':
                raise
            raise ConflictError('You have already answered this.') from None
        SurveyAnswer.objects.bulk_create([
            SurveyAnswer(response=r, question_id=a['questionId'], value_num=a['value'], choices=a['choices'],
                         text_value=a['text']) for a in answer_rows])
    # Polls show how everyone voted straight after voting (subject to the anonymity threshold).
    if s.kind == Survey.Kind.POLL:
        return {'ok': True, 'results': survey_results(s, _audience_size(s))}
    return {'ok': True}


def results(user: AuthUser, id_: str) -> dict:
    """Results: HR always; for polls, anyone who has voted."""
    id_ = parse_uuid(id_)
    s = survey_in_company(id_, company_id_of(user))
    if not is_hr(user) and (s.kind != Survey.Kind.POLL or not has_answered(id_, user.id)):
        raise NotFoundError('results are not available to you')
    return {'survey': public_survey(s), 'results': survey_results(s, _audience_size(s))}


def _num_str(v) -> str:
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def results_csv(user: AuthUser, id_: str) -> str:
    """Aggregated results as CSV (never one row per anonymous person)."""
    from hajiri.web.csv import cell_text
    _require_hr(user)
    id_ = parse_uuid(id_)
    s = survey_in_company(id_, company_id_of(user))
    out = survey_results(s, _audience_size(s))
    lines: list[list[str]] = [['Survey', s.title], ['Responses', str(out['responses'])], ['Audience', str(out['audience'])],
                              ['Anonymous', 'Yes' if s.anonymous else 'No'], []]
    if out['hidden']:
        lines.append([f'Results are hidden until at least {s.min_group} people answer.'])
    lines.append(['Question', 'Type', 'Answer', 'Count', 'Average', 'eNPS', 'Name'])
    for q in out['questions']:
        for d in q.get('distribution') or []:
            lines.append([q['prompt'], q['kind'], d['label'], str(d['count']), '', '', ''])
        if 'average' in q:
            lines.append([q['prompt'], q['kind'], 'Average', '', _num_str(q['average']),
                          str(q['enps']['score']) if q.get('enps') else '', ''])
        for g in q.get('breakdown') or []:
            lines.append([q['prompt'], q['kind'], f"Group: {g['group']}", str(g['responses']), _num_str(g['average']), '', ''])
        for t in q.get('texts') or []:
            lines.append([q['prompt'], q['kind'], t['text'], '', '', '', t.get('name', '')])
    buf = io.StringIO()
    buf.write('﻿')
    w = csv.writer(buf, lineterminator='\r\n')
    for line in lines:
        w.writerow([cell_text(c) for c in line])
    return buf.getvalue()


# ---------------------------------------------------------------- anonymous suggestion box

# In-memory guard against flooding the anonymous suggestion box (identity is never stored with a suggestion).
_suggestion_hits: dict[str, list[float]] = {}


def suggestion_allowed(user_id: str, at: float | None = None) -> bool:
    at = time.time() if at is None else at
    recent = [t for t in _suggestion_hits.get(user_id, []) if at - t < 86_400]
    if len(recent) >= 5:
        _suggestion_hits[user_id] = recent
        return False
    recent.append(at)
    _suggestion_hits[user_id] = recent
    return True


def suggest(user: AuthUser, body: dict) -> dict:
    b = validate(ser.SuggestIn, body)
    company = company_id_of(user)
    if not suggestion_allowed(user.id):
        raise ConflictError('You have sent several suggestions today. Please try again tomorrow.')
    # Deliberately no author and no time of day: only the company and the day it arrived.
    Suggestion.objects.create(company_id=company, category=b['category'], body=b['body'], received_on=npt_today())
    return {'ok': True}


_SUGGESTION_FIELDS = ('id', 'category', 'body', 'reply')
_SUGGESTION_ALIASES = {'receivedOn': F('received_on'), 'repliedAt': F('replied_at')}


def _suggestion_rows(qs, *extra: str, **aliases) -> list[dict]:
    out = list(qs.values(*_SUGGESTION_FIELDS, *extra, **_SUGGESTION_ALIASES, **aliases))
    for s in out:
        s['id'] = str(s['id'])
    return out


def public_suggestions(user: AuthUser) -> list[dict]:
    """Answered suggestions HR chose to share with everyone."""
    return _suggestion_rows(Suggestion.objects.for_company(company_id_of(user))
                            .filter(is_public=True, status=Suggestion.Status.ANSWERED).order_by('-replied_at')[:50])


def list_suggestions(user: AuthUser, query: dict | None = None) -> list[dict]:
    _require_hr(user)
    q = validate(ser.SuggestionsQuery, query or {})
    qs = SuggestionFilter(q, queryset=Suggestion.objects.for_company(company_id_of(user))).qs
    return _suggestion_rows(qs.order_by('-received_on', 'status')[:300], 'status', isPublic=F('is_public'))


def get_suggestion(user: AuthUser, id_: str) -> dict:
    """One suggestion of the company, in the list shape (for HR's reply form)."""
    _require_hr(user)
    id_ = parse_uuid(id_)
    found = _suggestion_rows(Suggestion.objects.for_company(company_id_of(user)).filter(pk=id_), 'status',
                             isPublic=F('is_public'))
    if not found:
        raise NotFoundError('suggestion not found')
    return found[0]


def update_suggestion(user: AuthUser, id_: str, body: dict) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    b = validate(ser.SuggestionPatchIn, body)
    status = Suggestion.Status.ANSWERED if b.get('reply') else b.get('status')
    changes: dict[str, Any] = {}
    if status:
        changes['status'] = status
    if b.get('reply'):
        changes.update(reply=b['reply'], replied_by_id=user.id, replied_at=Now())
    if 'isPublic' in b:
        changes['is_public'] = b['isPublic']
    qs = Suggestion.objects.for_company(company_id_of(user)).filter(pk=id_)
    with transaction.atomic():
        if not (qs.update(**changes) if changes else qs.exists()):
            raise NotFoundError('suggestion not found')
        after = {'status': status, 'replied': bool(b.get('reply'))}
        if 'isPublic' in b:
            after['isPublic'] = b['isPublic']
        audit('update', 'suggestion', actor_id=user.id, row_id=id_, after=after)
    return {'ok': True}


# ---------------------------------------------------------------- audience pickers for the forms

def audience_options(user: AuthUser) -> dict:
    """Branches and departments of the company, for the HR audience pickers."""
    company = company_id_of(user)
    branches = [{'id': str(i), 'name': n} for i, n in
                Branch.objects.for_company(company).order_by('name_en').values_list('id', 'name_en')]
    units = [{'id': str(i), 'name': n} for i, n in
             OrgUnit.objects.for_company(company).order_by('name_en').values_list('id', 'name_en')]
    return {'branches': branches, 'orgUnits': units}
