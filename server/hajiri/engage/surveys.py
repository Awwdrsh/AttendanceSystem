"""
Surveys and polls: question and answer validation, aggregated results with anonymity thresholds, and the daily job
(port of apps/api/src/modules/engage/surveys.ts).
"""
from __future__ import annotations

import secrets
from datetime import date, timedelta
from typing import Any

from django.db import transaction
from django.db.models import F
from django.db.models.functions import Now

from hajiri.core.bsdate import bs_label
from hajiri.core.numbers import round_half_up
from hajiri.core.crypto import FieldCipher, field_cipher
from hajiri.core.errors import BadRequestError, ValidationError
from hajiri.db.models import Survey, SurveyAnswer, SurveyQuestion, SurveyResponse

from .audience import audience_members
from .serializers import QUESTION_KINDS  # noqa: F401  (re-exported)

Kind = SurveyQuestion.Kind
CHOICE_KINDS = (Kind.SINGLE, Kind.MULTI)


def question_issues(q: dict, path: list) -> list[tuple[list, str]]:
    """The questionSchema superRefine: choice questions need two different options."""
    out = []
    if q['kind'] in CHOICE_KINDS:
        if len(q['options']) < 2:
            out.append((path + ['options'], 'choice questions need at least two options'))
        if len({o.lower() for o in q['options']}) != len(q['options']):
            out.append((path + ['options'], 'options must be different'))
    return out


def raise_issues(issues: list[tuple[list, str]]) -> None:
    """Problems found by the refinements (after the serializer accepted the shape), in the TS issue shape."""
    if issues:
        raise ValidationError([{'path': '.'.join(str(p) for p in path), 'message': message} for path, message in issues])


def respondent_key(cipher: FieldCipher, survey_id: str, user_id: str) -> str:
    """Keyed hash of (survey, user): stops double answers without storing who answered an anonymous survey."""
    return cipher.blind_index(f'survey:{survey_id}:{user_id}')


def questions_of(survey_id: str) -> list[dict]:
    out = list(SurveyQuestion.objects.filter(survey_id=survey_id).order_by('position')
               .values('id', 'survey_id', 'position', 'kind', 'prompt', 'options', 'required'))
    for q in out:
        q['id'] = str(q['id'])
        q['survey_id'] = str(q['survey_id'])
        q['options'] = list(q['options'] or [])
    return out


def iso(d: Any) -> str | None:
    if d is None:
        return None
    return d.isoformat() if isinstance(d, date) else str(d)[:10]


def is_open(s: Survey, today: str) -> bool:
    return s.status == Survey.Status.PUBLISHED and iso(s.opens_on) <= today and iso(s.closes_on) >= today


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
        if kind == Kind.RATING:
            v = a.get('value')
            if v is None or v < 1 or v > 5:
                raise BadRequestError(f"Pick a rating from 1 to 5 for: {q['prompt']}")
            out.append({'questionId': q['id'], 'value': v, 'choices': None, 'text': None})
        elif kind == Kind.ENPS:
            v = a.get('value')
            if v is None or v < 0 or v > 10:
                raise BadRequestError(f"Pick a score from 0 to 10 for: {q['prompt']}")
            out.append({'questionId': q['id'], 'value': v, 'choices': None, 'text': None})
        elif kind == Kind.SINGLE:
            c = a.get('choices') or []
            if len(c) != 1 or c[0] not in q['options']:
                raise BadRequestError(f"Pick one option for: {q['prompt']}")
            out.append({'questionId': q['id'], 'value': None, 'choices': c, 'text': None})
        elif kind == Kind.MULTI:
            c = list(dict.fromkeys(a.get('choices') or []))
            if not c or any(x not in q['options'] for x in c):
                raise BadRequestError(f"Pick from the listed options for: {q['prompt']}")
            out.append({'questionId': q['id'], 'value': None, 'choices': c, 'text': None})
        elif kind == Kind.TEXT:
            if not text:
                raise BadRequestError(f"Please answer: {q['prompt']}")
            out.append({'questionId': q['id'], 'value': None, 'choices': None, 'text': text})
    return out


# ---------------------------------------------------------------- results



def _num(x: float) -> int | float:
    return int(x) if float(x).is_integer() else x


def avg(xs: list[float]) -> int | float | None:
    return _num(round_half_up(sum(xs) / len(xs) * 100) / 100) if xs else None


def enps_of(values: list[int]) -> dict | None:
    if not values:
        return None
    promoters = sum(1 for v in values if v >= 9)
    detractors = sum(1 for v in values if v <= 6)
    return {'score': round_half_up((promoters - detractors) / len(values) * 100), 'promoters': promoters,
            'passives': len(values) - promoters - detractors, 'detractors': detractors}


def shuffle(xs: list) -> list:
    """A copy in random order from the OS CSPRNG, so text answers never come back in submission order."""
    a = list(xs)
    secrets.SystemRandom().shuffle(a)
    return a


def survey_results(s: Survey, audience_size: int) -> dict:
    """
    Aggregated results. Anonymous surveys show nothing until `min_group` responses arrive; department breakdowns only
    list groups that reach the minimum (smaller groups are pooled into "Other groups" when that pool is large enough);
    text answers are shuffled and carry no names, dates or ids.
    """
    sid = str(s.id)
    questions = questions_of(sid)
    resp = list(SurveyResponse.objects.filter(survey_id=sid)
                .values('id', 'org_unit_id', unit_name=F('org_unit__name_en'), employee_name=F('employee__name_en')))
    n = len(resp)
    mg = s.min_group
    if s.anonymous and n < mg:
        return {'responses': n, 'audience': audience_size, 'hidden': True, 'minGroup': mg, 'questions': []}
    # Live results of an open anonymous survey would let HR see how one person answered by comparing before and after.
    if s.anonymous and s.kind == Survey.Kind.SURVEY and s.status != Survey.Status.CLOSED:
        return {'responses': n, 'audience': audience_size, 'hidden': True, 'hiddenUntilClose': True, 'minGroup': mg,
                'questions': []}
    ans = list(SurveyAnswer.objects.filter(response__survey_id=sid)
               .values('response_id', 'question_id', 'value_num', 'choices', 'text_value'))
    resp_by_id = {str(r['id']): r for r in resp}
    results = []
    for q in questions:
        qrows = [a for a in ans if str(a['question_id']) == q['id']]
        base: dict[str, Any] = {'questionId': q['id'], 'kind': q['kind'], 'prompt': q['prompt'], 'options': q['options'],
                                'answered': len(qrows)}
        if q['kind'] in (Kind.RATING, Kind.ENPS):
            vals = [a['value_num'] for a in qrows if a['value_num'] is not None]
            scale = range(1, 6) if q['kind'] == Kind.RATING else range(0, 11)
            base['distribution'] = [{'label': str(v), 'count': sum(1 for x in vals if x == v)} for v in scale]
            base['average'] = avg(vals)
            if q['kind'] == Kind.ENPS:
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
                                  'average': enps_of(v)['score'] if q['kind'] == Kind.ENPS else avg(v)})
                else:
                    pooled.extend(v)
            if len(pooled) >= mg and shown:
                shown.append({'group': 'Other groups', 'responses': len(pooled),
                              'average': enps_of(pooled)['score'] if q['kind'] == Kind.ENPS else avg(pooled)})
            base['breakdown'] = sorted(shown, key=lambda b: -b['responses']) if len(shown) > 1 else []
        elif q['kind'] in CHOICE_KINDS:
            base['distribution'] = [{'label': o, 'count': sum(1 for a in qrows if o in (a['choices'] or []))}
                                    for o in q['options']]
        else:
            texts = []
            for a in qrows:
                if not a['text_value']:
                    continue
                t = {'text': a['text_value']}
                if not s.anonymous:
                    t['name'] = (resp_by_id.get(str(a['response_id'])) or {}).get('employee_name') or ''
                texts.append(t)
            base['texts'] = shuffle(texts) if s.anonymous else texts
        results.append(base)
    return {'responses': n, 'audience': audience_size, 'hidden': False, 'minGroup': mg, 'questions': results}


# ---------------------------------------------------------------- daily job

def add_ad(ad: str, days: int) -> str:
    return (date.fromisoformat(ad) + timedelta(days=days)).isoformat()


def days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def members_of(s: Survey) -> list[dict]:
    """The people a survey's audience reaches in its company."""
    return audience_members(str(s.company_id), s.audience_kind, s.audience_id and str(s.audience_id), None)


def notify_opened(s: Survey) -> int:
    from hajiri.notifications.service import notify
    members = members_of(s)
    for m in members:
        if not m['user_id']:
            continue
        notify(company_id=str(s.company_id), user_id=m['user_id'], employee_id=m['employee_id'],
               template='engage.survey_open', title='New poll' if s.kind == Survey.Kind.POLL else 'New survey',
               body=f"{s.title}{' (anonymous)' if s.anonymous else ''}. Open until {bs_label(s.closes_on)}.",
               payload={'surveyId': str(s.id)})
    Survey.objects.filter(pk=s.pk).update(opened_notified_at=Now())
    return len(members)


def _next_round(s: Survey, today: str) -> None:
    """Start the next round of a recurring pulse on the following cycle date, as long as the first round (rounds
    missed entirely are skipped), with the same questions."""
    length = days_between(iso(s.opens_on), iso(s.closes_on))
    step = 7 if s.recurrence == Survey.Recurrence.WEEKLY else 30
    opens = add_ad(iso(s.opens_on), step)
    while add_ad(opens, length) < today:
        opens = add_ad(opens, step)
    nxt = Survey.objects.create(
        company_id=s.company_id, kind=s.kind, title=s.title, description=s.description, anonymous=s.anonymous,
        min_group=s.min_group, audience_kind=s.audience_kind, audience_id=s.audience_id, status=Survey.Status.PUBLISHED,
        opens_on=opens, closes_on=add_ad(opens, length), recurrence=s.recurrence, series_id=s.series_id or s.id,
        created_by_id=s.created_by_id, published_at=Now())
    SurveyQuestion.objects.bulk_create([
        SurveyQuestion(survey=nxt, position=q.position, kind=q.kind, prompt=q.prompt, options=q.options, required=q.required)
        for q in SurveyQuestion.objects.filter(survey_id=s.pk).order_by('position')])


def run_survey_jobs(today: str, cipher: FieldCipher | None = None) -> dict:
    """
    Once a day: close surveys past their end date (and start the next round of a recurring pulse), announce surveys
    that opened today, and remind people who have not answered a survey closing tomorrow. A background job has no
    active company: each survey is handled within its own company.
    """
    from hajiri.notifications.service import notify
    cipher = cipher or field_cipher()
    out = {'closed': 0, 'started': 0, 'opened': 0, 'reminded': 0}
    day = date.fromisoformat(today)
    published = Survey.objects.filter(status=Survey.Status.PUBLISHED)
    for s in list(published.filter(closes_on__lt=day)):
        with transaction.atomic():
            # The status guard makes a second worker (or a close by hand meanwhile) skip the survey.
            if not Survey.objects.filter(pk=s.pk, status=Survey.Status.PUBLISHED).update(
                    status=Survey.Status.CLOSED, closed_at=Now()):
                continue
            out['closed'] += 1
            if s.recurrence == Survey.Recurrence.NONE:
                continue
            _next_round(s, today)
            out['started'] += 1
    for s in list(published.filter(opened_notified_at__isnull=True, opens_on__lte=day, closes_on__gte=day)):
        notify_opened(s)
        out['opened'] += 1
    for s in list(published.filter(reminded_at__isnull=True, closes_on=day + timedelta(days=1), opens_on__lt=day)):
        sid = str(s.id)
        answered = set(SurveyResponse.objects.filter(survey_id=sid).values_list('respondent_key', flat=True))
        for m in members_of(s):
            if not m['user_id'] or respondent_key(cipher, sid, m['user_id']) in answered:
                continue
            notify(company_id=str(s.company_id), user_id=m['user_id'], employee_id=m['employee_id'],
                   template='engage.survey_reminder', title='Survey closes tomorrow',
                   body=f"You have not answered \"{s.title}\" yet. It takes a minute.", payload={'surveyId': sid})
            out['reminded'] += 1
        Survey.objects.filter(pk=s.pk).update(reminded_at=Now())
    return out
