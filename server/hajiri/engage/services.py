"""
Engagement: announcements, kudos, celebrations, surveys and polls, and the anonymous suggestion box (port of
apps/api/src/modules/engage/routes.ts and service.ts). Everyone in the company reads the feed; posting announcements
is for HR (anyone) and managers (their own team); surveys are run by HR.

Functions take the signed-in AuthUser and, for writes, a body in the TS camelCase shape, validated with the same
rules and messages as the zod schemas. Results are dicts in the TS response shape.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from django.db import transaction

from engines.bs_calendar import BS_MONTH_NAMES_EN, BsRangeError, ad_to_bs, bs_month_bounds
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.clock import now as _now
from hajiri.core.clock import npt_today as _npt_today
from hajiri.core.crypto import field_cipher
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import (MISSING, Arr, Bool, DateTimeStr, Enum, Int, IsoDate, Obj, Str, Uuid, _Issue, _Issues,
                                parse, parse_uuid)

from .audience import (Placement, audience_members, audience_sql, audience_target_exists, is_hr,  # noqa: F401
                       placement_of)
from .surveys import (ANSWER_SCHEMA, QUESTION_SCHEMA, check_answers, is_open, iso, notify_opened, question_issues,
                      questions_of, raise_issues, respondent_key, survey_results)

REACTIONS = ('like', 'love', 'clap', 'celebrate', 'laugh')
TARGET_TYPES = ('kudos', 'announcement', 'celebration')
SUGGESTION_CATEGORIES = ('general', 'workplace', 'process', 'benefits', 'culture', 'other')
SUGGESTION_STATUSES = ('new', 'reviewed', 'answered', 'archived')


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


def _before(v: str | None) -> datetime:
    if v:
        return datetime.fromisoformat(v.replace('Z', '+00:00'))
    return _now() + timedelta(seconds=60)


def bs_month_of(ad: str) -> dict:
    """BS month that contains an AD date, as AD bounds."""
    b = ad_to_bs(ad)
    m = bs_month_bounds(b.year, b.month)
    return {'year': b.year, 'month': b.month, 'startAd': m['startAd'], 'endAd': m['endAd']}


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
    if one('SELECT 1 AS x FROM engage_value WHERE company_id = %s LIMIT 1', [company_id]):
        return
    for i, (name, description, emoji) in enumerate(DEFAULT_VALUES):
        execute("""INSERT INTO engage_value (company_id, name, description, emoji, sort_order) VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""", [company_id, name, description, emoji, i + 1])


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
    emps = rows("""SELECT e.id, e.name_en, e.designation, e.dob, e.join_date, p.show_birthday
                     FROM employee e LEFT JOIN engage_pref p ON p.employee_id = e.id
                    WHERE e.company_id = %s AND e.status = 'active' AND (e.dob IS NOT NULL OR e.join_date IS NOT NULL)""",
                [company_id])
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
    return sorted(out, key=lambda c: (c['daysAway'], c['name'].casefold()))


_CELEBRATION_ID = re.compile(r'^(birthday|anniversary|joined):([0-9a-f-]{36})(?::(\d{4}))?$')
# What PostgreSQL's uuid input accepts among 36 characters of [0-9a-f-]: 32 hex digits, a hyphen allowed after any group
# of four. Anything else the TS code sent to the database, which refused it as 'invalid value format' (22P02).
_PG_UUID = re.compile(r'^[0-9a-f]{4}(?:-?[0-9a-f]{4}){7}$')


def _pg_uuid_or_400(v: str) -> str:
    if not _PG_UUID.match(v):
        raise BadRequestError('invalid value format')
    return v


def celebration_target_ok(company_id: str, target_id: str) -> bool:
    """A celebration target id refers to an employee of the company (and, for birthdays, one who shows it)."""
    m = _CELEBRATION_ID.match(target_id)
    if not m:
        return False
    _pg_uuid_or_400(m[2])
    r = one("""SELECT p.show_birthday FROM employee e LEFT JOIN engage_pref p ON p.employee_id = e.id
                WHERE e.id = %s AND e.company_id = %s""", [m[2], company_id])
    if not r:
        return False
    return m[1] != 'birthday' or r['show_birthday'] is True


# ---------------------------------------------------------------- reactions and comments (shared)

def reaction_summary(company_id: str, user_id: str, type_: str, ids: list[str]) -> tuple[dict, dict, dict]:
    counts: dict[str, dict] = {}
    mine: dict[str, list] = {}
    comments: dict[str, int] = {}
    if not ids:
        return counts, mine, comments
    for x in rows("""SELECT target_id, emoji, count(*)::int AS n, bool_or(user_id = %s) AS mine FROM engage_reaction
                      WHERE company_id = %s AND target_type = %s AND target_id = ANY(%s::text[]) GROUP BY target_id, emoji
                      ORDER BY min(created_at)""",
                  [user_id, company_id, type_, list(ids)]):
        counts.setdefault(x['target_id'], {})[x['emoji']] = x['n']
        if x['mine']:
            mine.setdefault(x['target_id'], []).append(x['emoji'])
    for x in rows("""SELECT target_id, count(*)::int AS n FROM engage_comment
                      WHERE company_id = %s AND target_type = %s AND target_id = ANY(%s::text[]) AND deleted_at IS NULL
                      GROUP BY target_id""", [company_id, type_, list(ids)]):
        comments[x['target_id']] = x['n']
    return counts, mine, comments


def visible_announcement(user: AuthUser, id_: str) -> dict | None:
    company = company_id_of(user)
    params: list = [id_, company, user.id]
    cond = audience_sql('a', placement_of(user.employee_id), params, True)
    return one(f"""SELECT a.* FROM announcement a WHERE a.id = %s AND a.company_id = %s AND a.deleted_at IS NULL
                     AND ({'true' if is_hr(user) else 'false'} OR a.author_id = %s OR {cond})""", params)


def can_see_target(user: AuthUser, type_: str, id_: str) -> bool:
    """May this user see (and so react to or comment on) the target?"""
    company = company_id_of(user)
    if type_ == 'celebration':
        return celebration_target_ok(company, id_)
    if not re.fullmatch(r'[0-9a-f-]{36}', id_):
        return False
    _pg_uuid_or_400(id_)
    if type_ == 'kudos':
        return one('SELECT 1 AS x FROM kudos WHERE id = %s AND company_id = %s AND deleted_at IS NULL', [id_, company]) is not None
    return visible_announcement(user, id_) is not None


_TARGET = {'targetType': Enum(TARGET_TYPES), 'targetId': Str(min=1, max=120)}


def react(user: AuthUser, body: dict) -> dict:
    b = parse(Obj({**_TARGET, 'emoji': Enum(REACTIONS)}), body)
    company = company_id_of(user)
    if not can_see_target(user, b['targetType'], b['targetId']):
        raise NotFoundError('not found')
    with transaction.atomic():
        deleted = execute("""DELETE FROM engage_reaction WHERE company_id = %s AND target_type = %s AND target_id = %s
                              AND user_id = %s AND emoji = %s""",
                          [company, b['targetType'], b['targetId'], user.id, b['emoji']])
        if not deleted:
            execute("""INSERT INTO engage_reaction (company_id, target_type, target_id, user_id, emoji) VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""", [company, b['targetType'], b['targetId'], user.id, b['emoji']])
    counts, mine, comments = reaction_summary(company, user.id, b['targetType'], [b['targetId']])
    return {'reacted': not deleted, 'reactions': counts.get(b['targetId'], {}), 'mine': mine.get(b['targetId'], []),
            'comments': comments.get(b['targetId'], 0)}


def list_comments(user: AuthUser, query: dict) -> list[dict]:
    q = parse(Obj(_TARGET), query)
    company = company_id_of(user)
    if not can_see_target(user, q['targetType'], q['targetId']):
        raise NotFoundError('not found')
    out = rows("""SELECT c.id, c.body, c.created_at, c.user_id, coalesce(e.name_en, u.name) AS name
                    FROM engage_comment c JOIN app_user u ON u.id = c.user_id LEFT JOIN employee e ON e.id = u.employee_id
                   WHERE c.company_id = %s AND c.target_type = %s AND c.target_id = %s AND c.deleted_at IS NULL
                   ORDER BY c.created_at LIMIT 200""", [company, q['targetType'], q['targetId']])
    return [{'id': str(x['id']), 'body': x['body'], 'createdAt': x['created_at'], 'name': x['name'],
             'canDelete': str(x['user_id']) == user.id or is_hr(user)} for x in out]


def add_comment(user: AuthUser, body: dict) -> dict:
    b = parse(Obj({**_TARGET, 'body': Str(trim=True, min=1, max=1000)}), body)
    company = company_id_of(user)
    if not can_see_target(user, b['targetType'], b['targetId']):
        raise NotFoundError('not found')
    with transaction.atomic():
        c = one("""INSERT INTO engage_comment (company_id, target_type, target_id, user_id, body) VALUES (%s, %s, %s, %s, %s)
                   RETURNING id""", [company, b['targetType'], b['targetId'], user.id, b['body']])
        # Tell the people a kudos or celebration is about that someone wrote to them.
        recipients: list[str] = []
        if b['targetType'] == 'kudos':
            recipients = [str(x['user_id']) for x in rows(
                """SELECT u.id AS user_id FROM kudos_recipient kr JOIN app_user u ON u.employee_id = kr.employee_id AND u.status = 'active'
                    WHERE kr.kudos_id = %s
                   UNION SELECT giver_id FROM kudos WHERE id = %s""", [b['targetId'], b['targetId']])]
        elif b['targetType'] == 'celebration':
            recipients = [str(x['id']) for x in rows("SELECT id FROM app_user WHERE employee_id = %s AND status = 'active'",
                                                     [b['targetId'].split(':')[1]])]
        for uid in dict.fromkeys(recipients):
            if uid == user.id:
                continue
            _notify(company_id=company, user_id=uid, template='engage.comment',
                    title='Someone sent you a wish' if b['targetType'] == 'celebration' else 'New comment on kudos',
                    body=f"{user.name}: {b['body'][:140]}", payload={'targetType': b['targetType'], 'targetId': b['targetId']})
    return {'id': str(c['id']), 'targetType': b['targetType'], 'targetId': b['targetId']}


def delete_comment(user: AuthUser, id_: str) -> dict:
    id_ = parse_uuid(id_)
    company = company_id_of(user)
    c = one('SELECT user_id, target_type, target_id FROM engage_comment WHERE id = %s AND company_id = %s AND deleted_at IS NULL',
            [id_, company])
    if not c:
        raise NotFoundError('comment not found')
    if str(c['user_id']) != user.id and not is_hr(user):
        raise ForbiddenError('you can only remove your own comments')
    with transaction.atomic():
        execute('UPDATE engage_comment SET deleted_at = now() WHERE id = %s', [id_])
        if str(c['user_id']) != user.id:
            audit('delete', 'engage_comment', actor_id=user.id, row_id=id_)
    return {'ok': True, 'targetType': c['target_type'], 'targetId': c['target_id']}


# ---------------------------------------------------------------- announcements

_AUDIENCE = Enum(('all', 'branch', 'department', 'reports'), default='all')

ANNOUNCEMENT_BODY = Obj({
    'title': Str(trim=True, min=1, max=160),
    'body': Str(trim=True, min=1, max=8000),
    'audienceKind': _AUDIENCE,
    'audienceId': Uuid(nullish=True),
    'pinned': Bool(default=False),
    'requiresAck': Bool(default=False),
    'expiresOn': IsoDate(nullish=True),
    # HR only: also show it as a pop-up after sign-in, until acknowledged or until this date.
    'popup': Bool(default=False),
    'popupUntil': IsoDate(nullish=True),
})
ANNOUNCEMENT_PATCH = Obj({
    'title': Str(trim=True, min=1, max=160, optional=True),
    'body': Str(trim=True, min=1, max=8000, optional=True),
    'pinned': Bool(optional=True),
    'requiresAck': Bool(optional=True),
    'expiresOn': IsoDate(nullish=True),
    'popup': Bool(optional=True),
    'popupUntil': IsoDate(nullish=True),
}, strict=True)


def announcements_for(user: AuthUser, *, before: datetime | None = None, limit: int = 20, pinned_only: bool = False,
                      include_expired: bool = False) -> list[dict]:
    company = company_id_of(user)
    params: list = [user.id, company, before or _before(None), user.id]
    cond = audience_sql('a', placement_of(user.employee_id), params, True)
    params.append(limit)
    out = rows(f"""
        SELECT a.*, coalesce(e.name_en, u.name) AS author_name,
               EXISTS (SELECT 1 FROM announcement_ack k WHERE k.announcement_id = a.id AND k.user_id = %s) AS acked,
               coalesce(b.name_en, o.name_en) AS audience_name
          FROM announcement a JOIN app_user u ON u.id = a.author_id LEFT JOIN employee e ON e.id = a.author_employee_id
          LEFT JOIN branch b ON a.audience_kind = 'branch' AND b.id = a.audience_id
          LEFT JOIN org_unit o ON a.audience_kind = 'department' AND o.id = a.audience_id
         WHERE a.company_id = %s AND a.deleted_at IS NULL AND a.created_at < %s
           {'' if include_expired else 'AND (a.expires_on IS NULL OR a.expires_on >= (now() AT TIME ZONE \'Asia/Kathmandu\')::date)'}
           {'AND a.pinned' if pinned_only else ''}
           AND ({'true' if include_expired else 'false'} OR a.author_id = %s OR {cond})
         ORDER BY a.created_at DESC LIMIT %s""", params)
    ids = [str(a['id']) for a in out]
    counts, mine, comments = reaction_summary(company, user.id, 'announcement', ids)
    hr = is_hr(user)
    items = []
    for a in out:
        aid = str(a['id'])
        kind = a['audience_kind']
        label = ('Everyone' if kind == 'all' else f"{a['author_name']}'s team" if kind == 'reports'
                 else a['audience_name'] or '—')
        items.append({
            'type': 'announcement', 'id': aid, 'createdAt': a['created_at'], 'title': a['title'], 'body': a['body'],
            'authorName': a['author_name'], 'audienceKind': kind, 'audienceId': _s(a['audience_id']), 'audienceLabel': label,
            'pinned': a['pinned'], 'requiresAck': a['requires_ack'], 'acked': a['acked'], 'expiresOn': iso(a['expires_on']),
            'popup': a['popup'], 'popupUntil': iso(a['popup_until']),
            'reactions': counts.get(aid, {}), 'mine': mine.get(aid, []), 'comments': comments.get(aid, 0),
            'canEdit': str(a['author_id']) == user.id or hr,
        })
    return items


def list_announcements(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = parse(Obj({'before': DateTimeStr(optional=True), 'limit': Int(min=1, max=50, coerce=True, default=20),
                   'all': Enum(('1', '0'), optional=True)}), query or {})
    # HR may list every announcement (including expired ones and other teams') to manage them.
    every = q.get('all') == '1' and is_hr(user)
    return announcements_for(user, before=_before(q.get('before')), limit=q['limit'], include_expired=every)


def post_announcement(user: AuthUser, body: dict) -> dict:
    b = parse(ANNOUNCEMENT_BODY, body)
    company = company_id_of(user)
    hr = is_hr(user)
    if not has_role(user, 'hr', 'manager'):
        raise ForbiddenError('only HR and managers post announcements')
    if not hr and b['audienceKind'] != 'reports':
        raise ForbiddenError('managers post announcements to their own team')
    if b['audienceKind'] == 'reports' and not user.employee_id:
        raise BadRequestError('this account has no team')
    if not audience_target_exists(company, b['audienceKind'], b.get('audienceId')):
        raise BadRequestError('pick the branch or department this is for')
    if b.get('expiresOn') and b['expiresOn'] < npt_today():
        raise BadRequestError('the end date has already passed')
    audience_id = None if b['audienceKind'] in ('all', 'reports') else b.get('audienceId')
    popup = b['popup'] and hr
    with transaction.atomic():
        a = one("""INSERT INTO announcement (company_id, author_id, author_employee_id, title, body, audience_kind, audience_id,
                                             pinned, requires_ack, expires_on, popup, popup_until)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [company, user.id, user.employee_id, b['title'], b['body'], b['audienceKind'], audience_id,
                 b['pinned'] and hr, b['requiresAck'], b.get('expiresOn'), popup, b.get('popupUntil') if popup else None])
        aid = str(a['id'])
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
    b = parse(ANNOUNCEMENT_PATCH, body)
    a = visible_announcement(user, id_)
    if not a:
        raise NotFoundError('announcement not found')
    hr = is_hr(user)
    if str(a['author_id']) != user.id and not hr:
        raise ForbiddenError('only the author or HR can edit this')
    with transaction.atomic():
        execute("""UPDATE announcement SET title = coalesce(%s, title), body = coalesce(%s, body), pinned = coalesce(%s, pinned),
                          requires_ack = coalesce(%s, requires_ack), expires_on = CASE WHEN %s::boolean THEN %s::date ELSE expires_on END,
                          popup = coalesce(%s, popup), popup_until = CASE WHEN %s::boolean THEN %s::date ELSE popup_until END,
                          updated_at = now()
                    WHERE id = %s""",
                [b.get('title'), b.get('body'), None if 'pinned' not in b else (b['pinned'] and hr), b.get('requiresAck'),
                 'expiresOn' in b, b.get('expiresOn'), None if 'popup' not in b else (b['popup'] and hr),
                 'popupUntil' in b and hr, b.get('popupUntil'), id_])
        audit('update', 'announcement', actor_id=user.id, row_id=id_, before={'title': a['title']}, after=b)
    return {'ok': True}


def delete_announcement(user: AuthUser, id_: str) -> dict:
    id_ = parse_uuid(id_)
    a = visible_announcement(user, id_)
    if not a:
        raise NotFoundError('announcement not found')
    if str(a['author_id']) != user.id and not is_hr(user):
        raise ForbiddenError('only the author or HR can remove this')
    with transaction.atomic():
        execute('UPDATE announcement SET deleted_at = now() WHERE id = %s', [id_])
        audit('delete', 'announcement', actor_id=user.id, row_id=id_, before={'title': a['title']})
    return {'ok': True}


def ack_announcement(user: AuthUser, id_: str) -> dict:
    id_ = parse_uuid(id_)
    a = visible_announcement(user, id_)
    if not a:
        raise NotFoundError('announcement not found')
    if not a['requires_ack']:
        raise BadRequestError('this announcement does not ask for confirmation')
    execute('INSERT INTO announcement_ack (announcement_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', [id_, user.id])
    return {'ok': True}


def announcement_acks(user: AuthUser, id_: str) -> dict:
    """Who has and has not confirmed reading, for the author and HR."""
    id_ = parse_uuid(id_)
    company = company_id_of(user)
    a = visible_announcement(user, id_)
    if not a:
        raise NotFoundError('announcement not found')
    if str(a['author_id']) != user.id and not is_hr(user):
        raise ForbiddenError('only the author or HR can see confirmations')
    members = audience_members(company, a['audience_kind'], _s(a['audience_id']), _s(a['author_employee_id']))
    ack_by = {str(x['user_id']): x['acked_at'] for x in
              rows('SELECT user_id, acked_at FROM announcement_ack WHERE announcement_id = %s', [id_])}
    names = {str(n['id']): n for n in rows('SELECT id, name_en, emp_code FROM employee WHERE id = ANY(%s::uuid[])',
                                           [[m['employee_id'] for m in members]])}
    author = str(a['author_id'])
    people = [{'employeeId': m['employee_id'], 'name': (names.get(m['employee_id']) or {}).get('name_en') or '',
               'empCode': (names.get(m['employee_id']) or {}).get('emp_code') or '', 'ackedAt': ack_by.get(m['user_id'])}
              for m in members if m['user_id'] and m['user_id'] != author]
    people.sort(key=lambda p: (p['ackedAt'] is not None, p['name'].casefold()))
    return {'audience': len(people), 'acked': sum(1 for p in people if p['ackedAt']), 'people': people,
            'title': a['title']}


# ---------------------------------------------------------------- company values and settings

def list_values(user: AuthUser) -> dict:
    company = company_id_of(user)
    ensure_default_values(company)
    vals = rows("""SELECT id, name, description, emoji, active, sort_order AS "sortOrder" FROM engage_value
                    WHERE company_id = %s ORDER BY sort_order, name""", [company])
    for v in vals:
        v['id'] = str(v['id'])
    s = one('SELECT kudos_monthly_limit FROM engage_setting WHERE company_id = %s', [company])
    return {'values': vals, 'kudosMonthlyLimit': s['kudos_monthly_limit'] if s else None}


VALUE_BODY = Obj({
    'name': Str(trim=True, min=1, max=40), 'description': Str(trim=True, max=200, nullish=True),
    'emoji': Str(trim=True, min=1, max=8, default='⭐'), 'active': Bool(default=True),
    'sortOrder': Int(min=0, max=999, default=0),
})
VALUE_PATCH = Obj({
    'name': Str(trim=True, min=1, max=40, optional=True), 'description': Str(trim=True, max=200, nullish=True),
    'emoji': Str(trim=True, min=1, max=8, optional=True), 'active': Bool(optional=True),
    'sortOrder': Int(min=0, max=999, optional=True),
})


def _require_hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


def create_value(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    b = parse(VALUE_BODY, body)
    company = company_id_of(user)
    with transaction.atomic():
        v = one("""INSERT INTO engage_value (company_id, name, description, emoji, active, sort_order) VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING id""", [company, b['name'], b.get('description'), b['emoji'], b['active'], b['sortOrder']])
        audit('create', 'engage_value', actor_id=user.id, row_id=str(v['id']), after=b)
    return {'id': str(v['id'])}


def update_value(user: AuthUser, id_: str, body: dict) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    b = parse(VALUE_PATCH, body)
    company = company_id_of(user)
    with transaction.atomic():
        n = execute("""UPDATE engage_value SET name = coalesce(%s, name), description = CASE WHEN %s::boolean THEN %s ELSE description END,
                              emoji = coalesce(%s, emoji), active = coalesce(%s, active), sort_order = coalesce(%s, sort_order)
                        WHERE id = %s AND company_id = %s""",
                    [b.get('name'), 'description' in b, b.get('description'), b.get('emoji'), b.get('active'),
                     b.get('sortOrder'), id_, company])
        if not n:
            raise NotFoundError('value not found')
        audit('update', 'engage_value', actor_id=user.id, row_id=id_, after=b)
    return {'ok': True}


def save_settings(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    b = parse(Obj({'kudosMonthlyLimit': Int(min=1, max=100, nullable=True)}), body)
    company = company_id_of(user)
    with transaction.atomic():
        execute("""INSERT INTO engage_setting (company_id, kudos_monthly_limit) VALUES (%s, %s)
                   ON CONFLICT (company_id) DO UPDATE SET kudos_monthly_limit = EXCLUDED.kudos_monthly_limit, updated_at = now()""",
                [company, b['kudosMonthlyLimit']])
        audit('update', 'engage_setting', actor_id=user.id, row_id=company, after=b)
    return {'ok': True}


# ---------------------------------------------------------------- kudos

def colleagues(user: AuthUser) -> list[dict]:
    """Colleagues to pick from when giving kudos: name, code and designation only."""
    out = rows("""SELECT id, name_en AS name, emp_code AS "empCode", designation FROM employee
                   WHERE company_id = %s AND status = 'active' ORDER BY name_en""", [company_id_of(user)])
    for c in out:
        c['id'] = str(c['id'])
    return out


def kudos_items(user: AuthUser, *, before: datetime, limit: int, employee_id: str | None = None) -> list[dict]:
    company = company_id_of(user)
    params: list = [company, before]
    where = 'AND k.created_at < %s'
    if employee_id:
        params += [employee_id, employee_id]
        where += (' AND (k.giver_employee_id = %s OR EXISTS (SELECT 1 FROM kudos_recipient kr WHERE kr.kudos_id = k.id'
                  ' AND kr.employee_id = %s))')
    params.append(limit)
    out = rows(f"""
        SELECT k.id, k.created_at, k.message, k.giver_id, k.giver_employee_id, g.name_en AS giver_name, g.designation AS giver_designation,
               k.value_id, v.name AS value_name, v.emoji AS value_emoji,
               (SELECT json_agg(json_build_object('employeeId', e.id, 'name', e.name_en, 'designation', e.designation) ORDER BY e.name_en)
                  FROM kudos_recipient kr JOIN employee e ON e.id = kr.employee_id WHERE kr.kudos_id = k.id) AS recipients
          FROM kudos k JOIN employee g ON g.id = k.giver_employee_id LEFT JOIN engage_value v ON v.id = k.value_id
         WHERE k.company_id = %s AND k.deleted_at IS NULL {where}
         ORDER BY k.created_at DESC LIMIT %s""", params)
    ids = [str(x['id']) for x in out]
    counts, mine, comments = reaction_summary(company, user.id, 'kudos', ids)
    hr = is_hr(user)
    return [{
        'type': 'kudos', 'id': str(x['id']), 'createdAt': x['created_at'], 'message': x['message'],
        'giver': {'employeeId': str(x['giver_employee_id']), 'name': x['giver_name'], 'designation': x['giver_designation']},
        'recipients': x['recipients'] or [],
        'value': {'id': str(x['value_id']), 'name': x['value_name'], 'emoji': x['value_emoji']} if x['value_id'] else None,
        'reactions': counts.get(str(x['id']), {}), 'mine': mine.get(str(x['id']), []), 'comments': comments.get(str(x['id']), 0),
        'canDelete': str(x['giver_id']) == user.id or hr,
    } for x in out]


def list_kudos(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = parse(Obj({'before': DateTimeStr(optional=True), 'limit': Int(min=1, max=50, coerce=True, default=20),
                   'employeeId': Uuid(optional=True)}), query or {})
    return kudos_items(user, before=_before(q.get('before')), limit=q['limit'], employee_id=q.get('employeeId'))


def give_kudos(user: AuthUser, body: dict) -> dict:
    b = parse(Obj({'recipientIds': Arr(Uuid(), min=1, max=10), 'valueId': Uuid(nullish=True),
                   'message': Str(trim=True, min=3, max=1000)}), body)
    company = company_id_of(user)
    giver = user.employee_id
    if not giver:
        raise ForbiddenError('only employees can give kudos')
    recipients = list(dict.fromkeys(b['recipientIds']))
    if giver in recipients:
        raise BadRequestError('You cannot give kudos to yourself.')
    emps = rows("SELECT id FROM employee WHERE id = ANY(%s::uuid[]) AND company_id = %s AND status = 'active'", [recipients, company])
    if len(emps) != len(recipients):
        raise BadRequestError('Pick colleagues from your company.')
    if b.get('valueId'):
        if not one('SELECT 1 AS x FROM engage_value WHERE id = %s AND company_id = %s AND active', [b['valueId'], company]):
            raise BadRequestError('Pick one of the company values.')
    with transaction.atomic():
        # Serialise per giver so two quick clicks cannot both pass the monthly limit.
        execute('SELECT pg_advisory_xact_lock(hashtext(%s))', [f'kudos:{giver}'])
        lim = one('SELECT kudos_monthly_limit FROM engage_setting WHERE company_id = %s', [company])
        limit = lim['kudos_monthly_limit'] if lim else None
        if limit:
            m = bs_month_of(npt_today())
            used = one("""SELECT count(*)::int AS n FROM kudos WHERE giver_employee_id = %s AND deleted_at IS NULL
                            AND (created_at AT TIME ZONE 'Asia/Kathmandu')::date BETWEEN %s AND %s""",
                       [giver, m['startAd'], m['endAd']])['n']
            if used >= limit:
                raise ConflictError(f'You have given all {limit} kudos for this month. You can give more next month.')
        k = one('INSERT INTO kudos (company_id, giver_id, giver_employee_id, value_id, message) VALUES (%s, %s, %s, %s, %s) RETURNING id',
                [company, user.id, giver, b.get('valueId'), b['message']])
        kid = str(k['id'])
        for e in recipients:
            execute('INSERT INTO kudos_recipient (kudos_id, employee_id) VALUES (%s, %s)', [kid, e])
        for u in rows("SELECT id, employee_id FROM app_user WHERE employee_id = ANY(%s::uuid[]) AND status = 'active'", [recipients]):
            _notify(company_id=company, user_id=str(u['id']), employee_id=str(u['employee_id']), template='engage.kudos',
                    title=f'{user.name} gave you kudos', body=b['message'][:200], payload={'kudosId': kid}, channels=('push',))
    return {'id': kid}


def delete_kudos(user: AuthUser, id_: str) -> dict:
    id_ = parse_uuid(id_)
    k = one('SELECT giver_id FROM kudos WHERE id = %s AND company_id = %s AND deleted_at IS NULL', [id_, company_id_of(user)])
    if not k:
        raise NotFoundError('kudos not found')
    if str(k['giver_id']) != user.id and not is_hr(user):
        raise ForbiddenError('only the giver or HR can remove kudos')
    with transaction.atomic():
        execute('UPDATE kudos SET deleted_at = now() WHERE id = %s', [id_])
        audit('delete', 'kudos', actor_id=user.id, row_id=id_)
    return {'ok': True}


def leaderboard(user: AuthUser, query: dict | None = None) -> dict:
    """Most appreciated people, most generous givers and value counts for a BS month (default: this month)."""
    q = parse(Obj({'bsYear': Int(min=2000, max=2100, coerce=True, optional=True),
                   'bsMonth': Int(min=1, max=12, coerce=True, optional=True)}), query or {})
    company = company_id_of(user)
    cur = bs_month_of(npt_today())
    year = q.get('bsYear', cur['year'])
    month = q.get('bsMonth', cur['month'])
    try:
        bounds = bs_month_bounds(year, month)
    except (BsRangeError, ValueError, KeyError):
        raise BadRequestError('that month is outside the supported calendar')
    p = {'co': company, 'from': bounds['startAd'], 'to': bounds['endAd']}
    in_month = ("k.company_id = %(co)s AND k.deleted_at IS NULL"
                " AND (k.created_at AT TIME ZONE 'Asia/Kathmandu')::date BETWEEN %(from)s AND %(to)s")

    def people(r):
        return [{'employeeId': str(x['id']), 'name': x['name'], 'designation': x['designation'], 'count': x['count']} for x in r]
    received = people(rows(f"""SELECT e.id, e.name_en AS name, e.designation, count(*)::int AS count
                                 FROM kudos k JOIN kudos_recipient kr ON kr.kudos_id = k.id JOIN employee e ON e.id = kr.employee_id
                                WHERE {in_month} GROUP BY e.id ORDER BY count DESC, e.name_en LIMIT 10""", p))
    given = people(rows(f"""SELECT e.id, e.name_en AS name, e.designation, count(*)::int AS count
                              FROM kudos k JOIN employee e ON e.id = k.giver_employee_id WHERE {in_month}
                             GROUP BY e.id ORDER BY count DESC, e.name_en LIMIT 10""", p))
    values = rows(f"""SELECT v.id, v.name, v.emoji, count(k.id)::int AS count FROM engage_value v
                        LEFT JOIN kudos k ON k.value_id = v.id AND {in_month}
                       WHERE v.company_id = %(co)s GROUP BY v.id ORDER BY count DESC, v.sort_order""", p)
    for v in values:
        v['id'] = str(v['id'])
    total = one(f'SELECT count(*)::int AS n FROM kudos k WHERE {in_month}', p)['n']
    return {'bsYear': year, 'bsMonth': month, 'total': total, 'received': received, 'given': given, 'values': values}


# ---------------------------------------------------------------- feed, celebrations, preferences, home card

def iso_instant(d: datetime) -> str:
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def feed(user: AuthUser, query: dict | None = None) -> dict:
    q = parse(Obj({'before': DateTimeStr(optional=True), 'limit': Int(min=1, max=50, coerce=True, default=20)}), query or {})
    company = company_id_of(user)
    before = _before(q.get('before'))
    limit = q['limit']
    kudos = kudos_items(user, before=before, limit=limit) if _feature_on(company, 'kudos') else []
    anns = announcements_for(user, before=before, limit=limit)
    items = sorted(kudos + anns, key=lambda i: i['createdAt'], reverse=True)[:limit]
    return {'items': items, 'nextBefore': iso_instant(items[-1]['createdAt']) if len(items) == limit else None}


def celebrations_for(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = parse(Obj({'days': Int(min=1, max=60, coerce=True, default=14)}), query or {})
    company = company_id_of(user)
    out = celebrations(company, npt_today(), q['days'])
    counts, mine, comments = reaction_summary(company, user.id, 'celebration', [c['targetId'] for c in out])
    return [{**c, 'reactions': counts.get(c['targetId'], {}), 'mine': mine.get(c['targetId'], []),
             'comments': comments.get(c['targetId'], 0), 'isMe': c['employeeId'] == user.employee_id} for c in out]


def get_prefs(user: AuthUser) -> dict:
    if not user.employee_id:
        return {'showBirthday': False, 'hasBirthday': False}
    p = one("""SELECT coalesce(p.show_birthday, false) AS show_birthday, e.dob FROM employee e
                 LEFT JOIN engage_pref p ON p.employee_id = e.id WHERE e.id = %s""", [user.employee_id])
    return {'showBirthday': bool(p and p['show_birthday']), 'hasBirthday': bool(p and p['dob'])}


def save_prefs(user: AuthUser, body: dict) -> dict:
    b = parse(Obj({'showBirthday': Bool()}), body)
    if not user.employee_id:
        raise ForbiddenError('only employees have celebration settings')
    execute("""INSERT INTO engage_pref (employee_id, show_birthday) VALUES (%s, %s)
               ON CONFLICT (employee_id) DO UPDATE SET show_birthday = EXCLUDED.show_birthday, updated_at = now()""",
            [user.employee_id, b['showBirthday']])
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

def public_survey(s: dict) -> dict:
    out = {
        'id': str(s['id']), 'kind': s['kind'], 'title': s['title'], 'description': s['description'], 'anonymous': s['anonymous'],
        'minGroup': s['min_group'], 'audienceKind': s['audience_kind'], 'audienceId': _s(s['audience_id']),
        'status': s['status'], 'opensOn': iso(s['opens_on']), 'closesOn': iso(s['closes_on']), 'recurrence': s['recurrence'],
        'seriesId': _s(s['series_id']), 'createdAt': s['created_at'], 'publishedAt': s['published_at'], 'closedAt': s['closed_at'],
    }
    if 'responses' in s:
        out['responses'] = s['responses']
    if 'question_count' in s:
        out['questionCount'] = s['question_count']
    return out


def survey_in_company(id_: str, company_id: str) -> dict:
    s = one('SELECT * FROM survey WHERE id = %s AND company_id = %s', [id_, company_id])
    if not s:
        raise NotFoundError('survey not found')
    return s


def in_audience(user: AuthUser, s: dict) -> bool:
    if not user.employee_id:
        return False
    params: list = [user.employee_id, s['id']]
    cond = audience_sql('s', placement_of(user.employee_id), params, False)
    return one(f"""SELECT 1 AS x FROM survey s JOIN employee e ON e.id = %s AND e.status = 'active' AND e.company_id = s.company_id
                    WHERE s.id = %s AND {cond}""", params) is not None


def has_answered(survey_id: str, user_id: str) -> bool:
    return one('SELECT 1 AS x FROM survey_response WHERE survey_id = %s AND respondent_key = %s',
               [survey_id, respondent_key(field_cipher(), str(survey_id), user_id)]) is not None


def open_surveys_for(user: AuthUser) -> list[dict]:
    """Surveys and polls open to me now, with whether I have answered."""
    if not user.employee_id:
        return []
    company = company_id_of(user)
    today = npt_today()
    params: list = [company, today, today]
    cond = audience_sql('s', placement_of(user.employee_id), params, False)
    out = rows(f"""SELECT s.*, (SELECT count(*)::int FROM survey_question q WHERE q.survey_id = s.id) AS question_count FROM survey s
                    WHERE s.company_id = %s AND s.status = 'published' AND s.opens_on <= %s::date AND s.closes_on >= %s::date
                      AND {cond}
                    ORDER BY s.closes_on, s.created_at""", params)
    cipher = field_cipher()
    keys = [respondent_key(cipher, str(x['id']), user.id) for x in out]
    answered = {str(d['survey_id']) for d in rows('SELECT survey_id FROM survey_response WHERE respondent_key = ANY(%s::text[])', [keys])}
    return [{'id': str(x['id']), 'kind': x['kind'], 'title': x['title'], 'description': x['description'],
             'anonymous': x['anonymous'], 'opensOn': iso(x['opens_on']), 'closesOn': iso(x['closes_on']),
             'questionCount': x['question_count'], 'answered': str(x['id']) in answered} for x in out]


def manage_surveys(user: AuthUser) -> list[dict]:
    _require_hr(user)
    out = rows("""SELECT s.*, (SELECT count(*)::int FROM survey_response r WHERE r.survey_id = s.id) AS responses,
                         (SELECT count(*)::int FROM survey_question q WHERE q.survey_id = s.id) AS question_count
                    FROM survey s WHERE s.company_id = %s ORDER BY s.created_at DESC LIMIT 200""", [company_id_of(user)])
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


SURVEY_BODY = Obj({
    'kind': Enum(('survey', 'poll'), default='survey'),
    'title': Str(trim=True, min=1, max=160),
    'description': Str(trim=True, max=2000, nullish=True),
    'anonymous': Bool(default=True),
    'minGroup': Int(min=3, max=50, default=5),
    'audienceKind': Enum(('all', 'branch', 'department'), default='all'),
    'audienceId': Uuid(nullish=True),
    'opensOn': IsoDate(),
    'closesOn': IsoDate(),
    'recurrence': Enum(('none', 'weekly', 'monthly'), default='none'),
    'questions': Arr(QUESTION_SCHEMA, min=1, max=30),
})


def parse_survey_body(body: Any) -> dict:
    """surveyBody with its superRefine: choice questions need two different options, closing on or after opening,
    a poll has exactly one choice question."""
    b = parse(SURVEY_BODY, body)
    issues: list[_Issue] = []
    for i, q in enumerate(b['questions']):
        issues += question_issues(q, ['questions', i])
    if b['closesOn'] < b['opensOn']:
        issues.append(_Issue(['closesOn'], 'the closing day must be on or after the opening day'))
    if b['kind'] == 'poll' and (len(b['questions']) != 1 or b['questions'][0]['kind'] not in ('single', 'multi')):
        issues.append(_Issue(['questions'], 'a poll has exactly one choice question'))
    raise_issues(issues)
    return b


def _write_questions(survey_id: str, questions: list[dict]) -> None:
    import json
    for i, q in enumerate(questions):
        execute('INSERT INTO survey_question (survey_id, position, kind, prompt, options, required) VALUES (%s, %s, %s, %s, %s, %s)',
                [survey_id, i + 1, q['kind'], q['prompt'],
                 json.dumps(q['options'] if q['kind'] in ('single', 'multi') else [], ensure_ascii=False), q['required']])


def create_survey(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    b = parse_survey_body(body)
    company = company_id_of(user)
    if not audience_target_exists(company, b['audienceKind'], b.get('audienceId')):
        raise BadRequestError('pick the branch or department this is for')
    with transaction.atomic():
        s = one("""INSERT INTO survey (company_id, kind, title, description, anonymous, min_group, audience_kind, audience_id,
                                       opens_on, closes_on, recurrence, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [company, b['kind'], b['title'], b.get('description'), b['anonymous'], b['minGroup'], b['audienceKind'],
                 None if b['audienceKind'] == 'all' else b.get('audienceId'), b['opensOn'], b['closesOn'], b['recurrence'],
                 user.id])
        sid = str(s['id'])
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
    if s['status'] == 'closed':
        raise ConflictError('this survey is closed')
    if s['status'] == 'published':
        b = parse(Obj({'closesOn': IsoDate()}, strict=True), body)
        if b['closesOn'] < iso(s['opens_on']) or b['closesOn'] < npt_today():
            raise BadRequestError('the new closing day must be today or later')
        with transaction.atomic():
            execute('UPDATE survey SET closes_on = %s, reminded_at = NULL WHERE id = %s', [b['closesOn'], id_])
            audit('update', 'survey', actor_id=user.id, row_id=id_, after=b)
        return {'ok': True}
    b = parse_survey_body(body)
    if not audience_target_exists(company, b['audienceKind'], b.get('audienceId')):
        raise BadRequestError('pick the branch or department this is for')
    with transaction.atomic():
        execute("""UPDATE survey SET kind = %s, title = %s, description = %s, anonymous = %s, min_group = %s, audience_kind = %s,
                          audience_id = %s, opens_on = %s, closes_on = %s, recurrence = %s WHERE id = %s""",
                [b['kind'], b['title'], b.get('description'), b['anonymous'], b['minGroup'], b['audienceKind'],
                 None if b['audienceKind'] == 'all' else b.get('audienceId'), b['opensOn'], b['closesOn'], b['recurrence'], id_])
        execute('DELETE FROM survey_question WHERE survey_id = %s', [id_])
        _write_questions(id_, b['questions'])
        audit('update', 'survey', actor_id=user.id, row_id=id_, after={'title': b['title']})
    return {'ok': True}


def delete_survey(user: AuthUser, id_: str) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    s = survey_in_company(id_, company_id_of(user))
    if s['status'] != 'draft':
        raise ConflictError('only drafts can be deleted; close a published survey instead')
    with transaction.atomic():
        execute('DELETE FROM survey_question WHERE survey_id = %s', [id_])
        execute('DELETE FROM survey WHERE id = %s', [id_])
        audit('delete', 'survey', actor_id=user.id, row_id=id_, before={'title': s['title']})
    return {'ok': True}


def publish_survey(user: AuthUser, id_: str) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    s = survey_in_company(id_, company_id_of(user))
    if s['status'] != 'draft':
        raise ConflictError(f"this survey is already {s['status']}")
    if iso(s['closes_on']) < npt_today():
        raise BadRequestError('the closing day has passed; change the dates first')
    with transaction.atomic():
        u = one("UPDATE survey SET status = 'published', published_at = now() WHERE id = %s AND status = 'draft' RETURNING *", [id_])
        if not u:
            raise ConflictError('this survey was just published')
        notified = notify_opened(u) if iso(u['opens_on']) <= npt_today() else 0
        audit('publish', 'survey', actor_id=user.id, row_id=id_)
    return {'notified': notified}


def close_survey(user: AuthUser, id_: str) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    s = survey_in_company(id_, company_id_of(user))
    if s['status'] != 'published':
        raise ConflictError('only an open survey can be closed')
    with transaction.atomic():
        # Closing by hand ends the series too: a recurring pulse stops here.
        execute("UPDATE survey SET status = 'closed', closed_at = now(), recurrence = 'none' WHERE id = %s", [id_])
        audit('close', 'survey', actor_id=user.id, row_id=id_)
    return {'ok': True}


def _audience_size(s: dict) -> int:
    return len(audience_members(str(s['company_id']), s['audience_kind'], _s(s['audience_id']), None))


def respond(user: AuthUser, id_: str, body: dict) -> dict:
    id_ = parse_uuid(id_)
    b = parse(Obj({'answers': Arr(ANSWER_SCHEMA, max=30)}), body)
    company = company_id_of(user)
    s = survey_in_company(id_, company)
    if not is_open(s, npt_today()) or not in_audience(user, s):
        raise NotFoundError('this survey is not open to you')
    answer_rows = check_answers(questions_of(id_), b['answers'])
    p = placement_of(user.employee_id)
    with transaction.atomic():
        ins = one("""INSERT INTO survey_response (survey_id, respondent_key, employee_id, org_unit_id, branch_id, submitted_on)
                     VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (survey_id, respondent_key) DO NOTHING RETURNING id""",
                  [id_, respondent_key(field_cipher(), id_, user.id), None if s['anonymous'] else user.employee_id,
                   p.org_unit_id, p.branch_id, npt_today()])
        if not ins:
            raise ConflictError('You have already answered this.')
        for a in answer_rows:
            execute('INSERT INTO survey_answer (response_id, question_id, value_num, choices, text_value) VALUES (%s, %s, %s, %s, %s)',
                    [ins['id'], a['questionId'], a['value'], a['choices'], a['text']])
    # Polls show how everyone voted straight after voting (subject to the anonymity threshold).
    if s['kind'] == 'poll':
        return {'ok': True, 'results': survey_results(s, _audience_size(s))}
    return {'ok': True}


def results(user: AuthUser, id_: str) -> dict:
    """Results: HR always; for polls, anyone who has voted."""
    id_ = parse_uuid(id_)
    s = survey_in_company(id_, company_id_of(user))
    if not is_hr(user) and (s['kind'] != 'poll' or not has_answered(id_, user.id)):
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
    from hajiri.web.csv import csv_cell
    _require_hr(user)
    id_ = parse_uuid(id_)
    s = survey_in_company(id_, company_id_of(user))
    out = survey_results(s, _audience_size(s))
    lines: list[list[str]] = [['Survey', s['title']], ['Responses', str(out['responses'])], ['Audience', str(out['audience'])],
                              ['Anonymous', 'Yes' if s['anonymous'] else 'No'], []]
    if out['hidden']:
        lines.append([f"Results are hidden until at least {s['min_group']} people answer."])
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
    return '﻿' + '\r\n'.join(','.join(csv_cell(c) for c in line) for line in lines) + '\r\n'


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
    b = parse(Obj({'category': Enum(SUGGESTION_CATEGORIES, default='general'), 'body': Str(trim=True, min=5, max=3000)}), body)
    company = company_id_of(user)
    if not suggestion_allowed(user.id):
        raise ConflictError('You have sent several suggestions today. Please try again tomorrow.')
    # Deliberately no author and no time of day: only the company and the day it arrived.
    execute('INSERT INTO suggestion (company_id, category, body, received_on) VALUES (%s, %s, %s, %s)',
            [company, b['category'], b['body'], npt_today()])
    return {'ok': True}


def public_suggestions(user: AuthUser) -> list[dict]:
    """Answered suggestions HR chose to share with everyone."""
    out = rows("""SELECT id, category, body, received_on AS "receivedOn", reply, replied_at AS "repliedAt" FROM suggestion
                   WHERE company_id = %s AND is_public AND status = 'answered' ORDER BY replied_at DESC LIMIT 50""",
               [company_id_of(user)])
    for s in out:
        s['id'] = str(s['id'])
    return out


def list_suggestions(user: AuthUser, query: dict | None = None) -> list[dict]:
    _require_hr(user)
    q = parse(Obj({'status': Enum(SUGGESTION_STATUSES, optional=True)}), query or {})
    out = rows("""SELECT id, category, body, received_on AS "receivedOn", status, reply, is_public AS "isPublic",
                         replied_at AS "repliedAt" FROM suggestion
                   WHERE company_id = %s AND (%s::text IS NULL OR status = %s) ORDER BY received_on DESC, status LIMIT 300""",
               [company_id_of(user), q.get('status'), q.get('status')])
    for s in out:
        s['id'] = str(s['id'])
    return out


def get_suggestion(user: AuthUser, id_: str) -> dict:
    """One suggestion of the company, in the list shape (for HR's reply form)."""
    _require_hr(user)
    id_ = parse_uuid(id_)
    s = one("""SELECT id, category, body, received_on AS "receivedOn", status, reply, is_public AS "isPublic",
                      replied_at AS "repliedAt" FROM suggestion WHERE id = %s AND company_id = %s""", [id_, company_id_of(user)])
    if not s:
        raise NotFoundError('suggestion not found')
    s['id'] = str(s['id'])
    return s


def update_suggestion(user: AuthUser, id_: str, body: dict) -> dict:
    _require_hr(user)
    id_ = parse_uuid(id_)
    b = parse(Obj({'status': Enum(SUGGESTION_STATUSES, optional=True), 'reply': Str(trim=True, min=1, max=3000, optional=True),
                   'isPublic': Bool(optional=True)}), body)
    company = company_id_of(user)
    status = 'answered' if b.get('reply') else b.get('status')
    with transaction.atomic():
        n = execute("""UPDATE suggestion SET status = coalesce(%s, status), reply = coalesce(%s, reply), is_public = coalesce(%s, is_public),
                              replied_by = CASE WHEN %s::text IS NOT NULL THEN %s::uuid ELSE replied_by END,
                              replied_at = CASE WHEN %s::text IS NOT NULL THEN now() ELSE replied_at END
                        WHERE id = %s AND company_id = %s""",
                    [status, b.get('reply'), b.get('isPublic'), b.get('reply'), user.id, b.get('reply'), id_, company])
        if not n:
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
    branches = [{'id': str(r['id']), 'name': r['name_en']} for r in
                rows('SELECT id, name_en FROM branch WHERE company_id = %s ORDER BY name_en', [company])]
    units = [{'id': str(r['id']), 'name': r['name_en']} for r in
             rows('SELECT id, name_en FROM org_unit WHERE company_id = %s ORDER BY name_en', [company])]
    return {'branches': branches, 'orgUnits': units}


