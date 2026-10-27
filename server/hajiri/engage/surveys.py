"""
Surveys and polls: question and answer validation, aggregated results with anonymity thresholds, and the daily job
(port of apps/api/src/modules/engage/surveys.ts).
"""
from __future__ import annotations

import math
import secrets
from datetime import date, timedelta
from typing import Any

from django.db import transaction

from hajiri.core.bsdate import bs_label
from hajiri.core.crypto import FieldCipher, field_cipher
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ValidationError
from hajiri.core.schema import Arr, Bool, Enum, Int, Obj, Str, Uuid, _Issue

from .audience import audience_members

QUESTION_KINDS = ('rating', 'enps', 'single', 'multi', 'text')

QUESTION_SCHEMA = Obj({
    'kind': Enum(QUESTION_KINDS),
    'prompt': Str(trim=True, min=1, max=300),
    'options': Arr(Str(trim=True, min=1, max=120), max=12, default=list),
    'required': Bool(default=True),
})

ANSWER_SCHEMA = Obj({
    'questionId': Uuid(),
    'value': Int(optional=True),
    'choices': Arr(Str(max=120), max=12, optional=True),
    'text': Str(max=2000, optional=True),
})


def question_issues(q: dict, path: list) -> list[_Issue]:
    """The questionSchema superRefine: choice questions need two different options."""
    out = []
    if q['kind'] in ('single', 'multi'):
        if len(q['options']) < 2:
            out.append(_Issue(path + ['options'], 'choice questions need at least two options'))
        if len({o.lower() for o in q['options']}) != len(q['options']):
            out.append(_Issue(path + ['options'], 'options must be different'))
    return out


def raise_issues(issues: list[_Issue]) -> None:
    if issues:
        raise ValidationError([{'path': '.'.join(str(p) for p in i.path), 'message': i.message} for i in issues])


def respondent_key(cipher: FieldCipher, survey_id: str, user_id: str) -> str:
    """Keyed hash of (survey, user): stops double answers without storing who answered an anonymous survey."""
    return cipher.blind_index(f'survey:{survey_id}:{user_id}')


def questions_of(survey_id: str) -> list[dict]:
    out = rows('SELECT * FROM survey_question WHERE survey_id = %s ORDER BY position', [survey_id])
    for q in out:
        q['id'] = str(q['id'])
        q['survey_id'] = str(q['survey_id'])
        q['options'] = list(q['options'] or [])
    return out


def iso(d: Any) -> str | None:
    if d is None:
        return None
    return d.isoformat() if isinstance(d, date) else str(d)[:10]


def is_open(s: dict, today: str) -> bool:
    return s['status'] == 'published' and iso(s['opens_on']) <= today and iso(s['closes_on']) >= today


def check_answers(questions: list[dict], answers: list[dict]) -> list[dict]:
    """Validate a full set of answers against the questions; returns the rows to store
    ([{'questionId', 'value', 'choices', 'text'}])."""
    by_id = {a['questionId']: a for a in answers}
    if len(by_id) != len(answers):
        raise BadRequestError('Each question can be answered once.')
    ids = {q['id'] for q in questions}
    for a in answers:
        if a['questionId'] not in ids:
            raise BadRequestError('An answer refers to a question that is not in this survey.')
    out = []
    for q in questions:
        a = by_id.get(q['id'])
        text = (a.get('text') or '').strip() if a else ''
        empty = not a or ('value' not in a and not a.get('choices') and not text)
        if empty:
            if q['required']:
                raise BadRequestError(f"Please answer: {q['prompt']}")
            continue
        kind = q['kind']
        if kind == 'rating':
            v = a.get('value')
            if v is None or v < 1 or v > 5:
                raise BadRequestError(f"Pick a rating from 1 to 5 for: {q['prompt']}")
            out.append({'questionId': q['id'], 'value': v, 'choices': None, 'text': None})
        elif kind == 'enps':
            v = a.get('value')
            if v is None or v < 0 or v > 10:
                raise BadRequestError(f"Pick a score from 0 to 10 for: {q['prompt']}")
            out.append({'questionId': q['id'], 'value': v, 'choices': None, 'text': None})
        elif kind == 'single':
            c = a.get('choices') or []
            if len(c) != 1 or c[0] not in q['options']:
                raise BadRequestError(f"Pick one option for: {q['prompt']}")
            out.append({'questionId': q['id'], 'value': None, 'choices': c, 'text': None})
        elif kind == 'multi':
            c = list(dict.fromkeys(a.get('choices') or []))
            if not c or any(x not in q['options'] for x in c):
                raise BadRequestError(f"Pick from the listed options for: {q['prompt']}")
            out.append({'questionId': q['id'], 'value': None, 'choices': c, 'text': None})
        elif kind == 'text':
            if not text:
                raise BadRequestError(f"Please answer: {q['prompt']}")
            out.append({'questionId': q['id'], 'value': None, 'choices': None, 'text': text})
    return out


# ---------------------------------------------------------------- results

def js_round(x: float) -> int:
    """Math.round: halves go up."""
    return math.floor(x + 0.5)


def _num(x: float) -> int | float:
    return int(x) if float(x).is_integer() else x


def avg(xs: list[float]) -> int | float | None:
    return _num(js_round(sum(xs) / len(xs) * 100) / 100) if xs else None


def enps_of(values: list[int]) -> dict | None:
    if not values:
        return None
    promoters = sum(1 for v in values if v >= 9)
    detractors = sum(1 for v in values if v <= 6)
    return {'score': js_round((promoters - detractors) / len(values) * 100), 'promoters': promoters,
            'passives': len(values) - promoters - detractors, 'detractors': detractors}


def shuffle(xs: list) -> list:
    """Fisher-Yates with a CSPRNG so text answers never come back in submission order."""
    a = list(xs)
    for i in range(len(a) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        a[i], a[j] = a[j], a[i]
    return a


def survey_results(s: dict, audience_size: int) -> dict:
    """
    Aggregated results. Anonymous surveys show nothing until `min_group` responses arrive; department breakdowns only
    list groups that reach the minimum (smaller groups are pooled into "Other groups" when that pool is large enough);
    text answers are shuffled and carry no names, dates or ids.
    """
    sid = str(s['id'])
    questions = questions_of(sid)
    resp = rows("""SELECT r.id, r.org_unit_id, o.name_en AS unit_name, e.name_en AS employee_name
                     FROM survey_response r LEFT JOIN org_unit o ON o.id = r.org_unit_id LEFT JOIN employee e ON e.id = r.employee_id
                    WHERE r.survey_id = %s""", [sid])
    n = len(resp)
    mg = s['min_group']
    if s['anonymous'] and n < mg:
        return {'responses': n, 'audience': audience_size, 'hidden': True, 'minGroup': mg, 'questions': []}
    # Live results of an open anonymous survey would let HR see how one person answered by comparing before and after.
    if s['anonymous'] and s['kind'] == 'survey' and s['status'] != 'closed':
        return {'responses': n, 'audience': audience_size, 'hidden': True, 'hiddenUntilClose': True, 'minGroup': mg,
                'questions': []}
    ans = rows("""SELECT a.response_id, a.question_id, a.value_num, a.choices, a.text_value
                    FROM survey_answer a JOIN survey_response r ON r.id = a.response_id WHERE r.survey_id = %s""", [sid])
    resp_by_id = {str(r['id']): r for r in resp}
    results = []
    for q in questions:
        qrows = [a for a in ans if str(a['question_id']) == q['id']]
        base: dict[str, Any] = {'questionId': q['id'], 'kind': q['kind'], 'prompt': q['prompt'], 'options': q['options'],
                                'answered': len(qrows)}
        if q['kind'] in ('rating', 'enps'):
            vals = [a['value_num'] for a in qrows if a['value_num'] is not None]
            scale = range(1, 6) if q['kind'] == 'rating' else range(0, 11)
            base['distribution'] = [{'label': str(v), 'count': sum(1 for x in vals if x == v)} for v in scale]
            base['average'] = avg(vals)
            if q['kind'] == 'enps':
                base['enps'] = enps_of(vals)
            groups: dict[str, list] = {}
            for a in qrows:
                r = resp_by_id.get(str(a['response_id']))
                g = (r or {}).get('unit_name') or 'No department'
                groups.setdefault(g, []).append(a['value_num'])
            shown, pooled = [], []
            for group, v in groups.items():
                if len(v) >= mg:
                    shown.append({'group': group, 'responses': len(v),
                                  'average': enps_of(v)['score'] if q['kind'] == 'enps' else avg(v)})
                else:
                    pooled.extend(v)
            if len(pooled) >= mg and shown:
                shown.append({'group': 'Other groups', 'responses': len(pooled),
                              'average': enps_of(pooled)['score'] if q['kind'] == 'enps' else avg(pooled)})
            base['breakdown'] = sorted(shown, key=lambda b: -b['responses']) if len(shown) > 1 else []
        elif q['kind'] in ('single', 'multi'):
            base['distribution'] = [{'label': o, 'count': sum(1 for a in qrows if o in (a['choices'] or []))}
                                    for o in q['options']]
        else:
            texts = []
            for a in qrows:
                if not a['text_value']:
                    continue
                t = {'text': a['text_value']}
                if not s['anonymous']:
                    t['name'] = (resp_by_id.get(str(a['response_id'])) or {}).get('employee_name') or ''
                texts.append(t)
            base['texts'] = shuffle(texts) if s['anonymous'] else texts
        results.append(base)
    return {'responses': n, 'audience': audience_size, 'hidden': False, 'minGroup': mg, 'questions': results}


# ---------------------------------------------------------------- daily job

def add_ad(ad: str, days: int) -> str:
    return (date.fromisoformat(ad) + timedelta(days=days)).isoformat()


def days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def notify_opened(s: dict) -> int:
    from hajiri.notifications.service import notify
    members = audience_members(str(s['company_id']), s['audience_kind'], s['audience_id'] and str(s['audience_id']), None)
    for m in members:
        if not m['user_id']:
            continue
        notify(company_id=str(s['company_id']), user_id=m['user_id'], employee_id=m['employee_id'],
               template='engage.survey_open', title='New poll' if s['kind'] == 'poll' else 'New survey',
               body=f"{s['title']}{' (anonymous)' if s['anonymous'] else ''}. Open until {bs_label(s['closes_on'])}.",
               payload={'surveyId': str(s['id'])})
    execute('UPDATE survey SET opened_notified_at = now() WHERE id = %s', [s['id']])
    return len(members)


def run_survey_jobs(today: str, cipher: FieldCipher | None = None) -> dict:
    """
    Once a day: close surveys past their end date (and start the next round of a recurring pulse), announce surveys
    that opened today, and remind people who have not answered a survey closing tomorrow.
    """
    from hajiri.notifications.service import notify
    cipher = cipher or field_cipher()
    out = {'closed': 0, 'started': 0, 'opened': 0, 'reminded': 0}
    due = rows("SELECT * FROM survey WHERE status = 'published' AND closes_on < %s::date", [today])
    for s in due:
        with transaction.atomic():
            if not execute("UPDATE survey SET status = 'closed', closed_at = now() WHERE id = %s AND status = 'published'", [s['id']]):
                continue
            out['closed'] += 1
            if s['recurrence'] == 'none':
                continue
            length = days_between(iso(s['opens_on']), iso(s['closes_on']))
            step = 7 if s['recurrence'] == 'weekly' else 30
            # Next round starts on the following cycle date and lasts as long as the first; rounds missed entirely are skipped.
            opens = add_ad(iso(s['opens_on']), step)
            while add_ad(opens, length) < today:
                opens = add_ad(opens, step)
            nxt = one("""INSERT INTO survey (company_id, kind, title, description, anonymous, min_group, audience_kind, audience_id, status,
                                             opens_on, closes_on, recurrence, series_id, created_by, published_at)
                         SELECT company_id, kind, title, description, anonymous, min_group, audience_kind, audience_id, 'published', %s, %s,
                                recurrence, coalesce(series_id, id), created_by, now() FROM survey WHERE id = %s RETURNING id""",
                      [opens, add_ad(opens, length), s['id']])
            execute("""INSERT INTO survey_question (survey_id, position, kind, prompt, options, required)
                       SELECT %s, position, kind, prompt, options, required FROM survey_question WHERE survey_id = %s""",
                    [nxt['id'], s['id']])
            out['started'] += 1
    opening = rows("""SELECT * FROM survey WHERE status = 'published' AND opened_notified_at IS NULL
                        AND opens_on <= %s::date AND closes_on >= %s::date""", [today, today])
    for s in opening:
        notify_opened(s)
        out['opened'] += 1
    closing = rows("""SELECT * FROM survey WHERE status = 'published' AND reminded_at IS NULL
                        AND closes_on = %s::date + 1 AND opens_on < %s::date""", [today, today])
    for s in closing:
        sid = str(s['id'])
        members = audience_members(str(s['company_id']), s['audience_kind'], s['audience_id'] and str(s['audience_id']), None)
        answered = {r['respondent_key'] for r in rows('SELECT respondent_key FROM survey_response WHERE survey_id = %s', [sid])}
        for m in members:
            if not m['user_id'] or respondent_key(cipher, sid, m['user_id']) in answered:
                continue
            notify(company_id=str(s['company_id']), user_id=m['user_id'], employee_id=m['employee_id'],
                   template='engage.survey_reminder', title='Survey closes tomorrow',
                   body=f"You have not answered \"{s['title']}\" yet. It takes a minute.", payload={'surveyId': sid})
            out['reminded'] += 1
        execute('UPDATE survey SET reminded_at = now() WHERE id = %s', [sid])
    return out

