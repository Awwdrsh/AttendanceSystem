"""The engage lists cost a fixed number of queries however many rows they show (no query per kudos, announcement,
survey, suggestion, celebration or audience member)."""
from datetime import date, timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext

from hajiri.db.models import Employee
from hajiri.engage import services as S
from tests.web.factories import htmx, make_employee, make_user

from .conftest import client_for

QUESTIONS = [{'kind': 'rating', 'prompt': 'How was the week?'}, {'kind': 'text', 'prompt': 'Anything else?',
                                                                  'required': False}]


def _add_rows(w, n: int, tag: str) -> None:
    """n more kudos (each with two recipients, a reaction and a comment), announcements, surveys with answers,
    suggestions and new joiners."""
    today = S.npt_today()
    for i in range(n):
        k = S.give_kudos(w.sita, {'recipientIds': [w.ram_id, w.extra[i % 5].id], 'message': f'Thanks {tag}{i}'})['id']
        S.react(w.ram, {'targetType': 'kudos', 'targetId': k, 'emoji': 'like'})
        S.add_comment(w.ram, {'targetType': 'kudos', 'targetId': k, 'body': 'Well done'})
        a = S.post_announcement(w.hr, {'title': f'News {tag}{i}', 'body': 'Read me', 'requiresAck': True})['id']
        S.ack_announcement(w.sita, a)
        sid = S.create_survey(w.hr, {'title': f'Pulse {tag}{i}', 'anonymous': False, 'opensOn': today,
                                     'closesOn': today, 'questions': QUESTIONS})['id']
        S.publish_survey(w.hr, sid)
        q = S.get_survey(w.sita, sid)['questions'][0]['id']
        S.respond(w.sita, sid, {'answers': [{'questionId': q, 'value': 4}]})
        S.suggest(w.extra[i % 5].user, {'body': f'Idea number {tag}{i}'})
        S._suggestion_hits.clear()
        e = make_employee(w.co, code=f'J{tag}{i}', name=f'Joiner {tag}{i}')
        Employee.objects.filter(pk=e.pk).update(join_date=date.fromisoformat(today) - timedelta(days=1))
        make_user(w.co, roles=['employee'], employee=e)


def _count(client, url: str) -> int:
    with CaptureQueriesContext(connection) as q:
        r = client.get(url, **htmx())
    assert r.status_code == 200, (url, r.status_code)
    return len(q)


PAGES = ['/_/engage/feed', '/_/engage/celebrations', '/_/engage/leaderboard', '/_/engage/surveys',
         '/engage/suggestions', '/_/engage/kudos/new', '/_/engage/values', '/_/engage/home']


def test_list_pages_cost_the_same_with_more_rows(world):
    w = world
    _add_rows(w, 2, 'a')
    hr, sita = client_for(w.hr), client_for(w.sita)
    who = {'/engage/suggestions': hr, '/_/engage/values': hr}
    for u in PAGES:  # warm up: first-use work (starter company values, cached switches) is not per row
        _count(who.get(u, sita), u)
    before = {u: _count(who.get(u, sita), u) for u in PAGES}
    before_hr_surveys = _count(hr, '/_/engage/surveys')
    _add_rows(w, 4, 'b')
    after = {u: _count(who.get(u, sita), u) for u in PAGES}
    assert after == before
    assert _count(hr, '/_/engage/surveys') == before_hr_surveys


def test_confirmations_cost_the_same_with_a_larger_audience(world):
    w = world
    a = S.post_announcement(w.hr, {'title': 'Read', 'body': 'x', 'requiresAck': True})['id']
    hr = client_for(w.hr)
    before = _count(hr, f'/_/engage/announcements/{a}/acks')
    for i in range(4):
        make_user(w.co, roles=['employee'], employee=make_employee(w.co, code=f'X{i}', name=f'Extra {i}'))
    assert _count(hr, f'/_/engage/announcements/{a}/acks') == before


def test_survey_list_counts_responses_and_questions_separately(world):
    """Responses and questions are counted on their own (not multiplied by a join)."""
    w = world
    _add_rows(w, 1, 'c')
    s = next(x for x in S.manage_surveys(w.hr) if x['title'] == 'Pulse c0')
    assert (s['responses'], s['questionCount']) == (1, 2)
