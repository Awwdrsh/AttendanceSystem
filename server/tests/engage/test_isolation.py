"""
Engage in a group of companies. Alpha Traders and Beta Foods are one group, Gamma Outside is not:

- every engage page, partial and action taking an id answers 404 for a record of another company, also to the group
  admin while they work in Alpha (they may switch to Beta, but are not working there); Beta's admin is the control;
- ids of another company inside a body (kudos recipients, a company value, an audience) are refused;
- feeds and lists show only the active company, and switching changes them;
- a person who works in Beta through a membership is reached there: announcements, kudos and surveys of Beta notify
  their account, and they answer Beta's surveys while working in Beta.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from django.test import Client

from hajiri.core import auth, companies
from hajiri.core.errors import BadRequestError
from hajiri.db.models import Branch, Company, CompanyMembership, NotificationOutbox
from hajiri.engage import services as S
from tests.web.factories import PW, htmx, make_company, make_employee, make_user, sign_in


@dataclass
class Group:
    alpha: Company
    beta: Company
    gamma: Company
    admin_a: auth.AuthUser      # admin of Alpha who started the group: group admin (admin in Beta too)
    admin_b: auth.AuthUser      # admin of Beta, with a Beta employee record
    admin_c: auth.AuthUser      # admin of Gamma, outside the group
    beta_colleague: auth.AuthUser
    dual: auth.AuthUser         # HR of Alpha (home), employee in Beta by membership
    dual_beta: object           # dual's Beta employee record

    def client(self, person, company=None) -> Client:
        c = Client()
        sign_in(c, person)
        if company is not None:
            r = c.post('/_/web/company/switch', {'company_id': str(company.id)}, **htmx())
            assert r.status_code == 204, r.content
        return c

    def working_in(self, person, company) -> auth.AuthUser:
        token = auth.login(person.email, PW).token
        user = auth.resolve_session(token)
        if str(company.id) != user.company_id:
            user = auth.switch_company(user, token, str(company.id))
        return user


@pytest.fixture
def group(db) -> Group:
    alpha = make_company('ALPHA', 'Alpha Traders')
    admin_a = make_user(alpha, roles=['admin'], employee=make_employee(alpha, name='Asha Admin'), email='admin@alpha.test')
    beta_id = companies.create_group_company(auth.resolve_session(auth.login(admin_a.email, PW).token),
                                             {'code': 'BETA', 'name_en': 'Beta Foods'})['id']
    beta = Company.objects.get(pk=beta_id)
    gamma = make_company('GAMMA', 'Gamma Outside')
    admin_b = make_user(beta, roles=['admin'], employee=make_employee(beta, name='Bimal Admin'), email='admin@beta.test')
    admin_c = make_user(gamma, roles=['admin'], employee=make_employee(gamma, name='Gita Admin'), email='admin@gamma.test')
    colleague = make_user(beta, roles=['employee'], employee=make_employee(beta, name='Bina Colleague'))
    dual = make_user(alpha, roles=['hr'], employee=make_employee(alpha, name='Dipak Dual'), email='dual@alpha.test')
    dual_beta = make_employee(beta, name='Dipak at Beta')
    CompanyMembership.objects.create(user_id=dual.id, company=beta, roles=['employee'], employee=dual_beta)
    return Group(alpha, beta, gamma, admin_a, admin_b, admin_c, colleague, dual, dual_beta)


# ---------------------------------------------------------------- records of Beta

def _today():
    return S.npt_today()


def announcement(g):
    return S.post_announcement(g.admin_b, {'title': 'Beta news', 'body': 'Hello Beta', 'requiresAck': True})['id']


def kudos(g):
    return S.give_kudos(g.admin_b, {'recipientIds': [g.beta_colleague.employee_id], 'message': 'Thanks for the help'})['id']


def comment(g):
    return S.add_comment(g.admin_b, {'targetType': 'kudos', 'targetId': kudos(g), 'body': 'Well done'})['id']


def value(g):
    return S.create_value(g.admin_b, {'name': 'Beta spirit'})['id']


QUESTIONS = [{'kind': 'rating', 'prompt': 'How was the week?'}]


def draft_survey(g):
    t = _today()
    return S.create_survey(g.admin_b, {'title': 'Beta pulse', 'anonymous': False, 'opensOn': t, 'closesOn': t,
                                       'questions': QUESTIONS})['id']


def open_survey(g):
    sid = draft_survey(g)
    S.publish_survey(g.admin_b, sid)
    return sid


def suggestion(g):
    S.suggest(g.admin_b, {'body': 'A canteen for Beta'})
    return S.list_suggestions(g.admin_b)[0]['id']


def beta_employee(g):
    return g.beta_colleague.employee_id


CASES = [
    # (name, method, url, maker, data)
    ('announcement pin', 'post', '/_/engage/announcements/{id}/pin', announcement, {'pinned': '1'}),
    ('announcement ack', 'post', '/_/engage/announcements/{id}/ack', announcement, {}),
    ('announcement acks', 'get', '/_/engage/announcements/{id}/acks', announcement, {}),
    ('announcement delete', 'post', '/_/engage/announcements/{id}/delete', announcement, {}),
    ('announcement reaction', 'post', '/_/engage/react', announcement,
     {'target_type': 'announcement', 'target_id': '{id}', 'emoji': 'like'}),
    ('announcement comments', 'get', '/_/engage/comments?target_type=announcement&target_id={id}', announcement, {}),
    ('kudos delete', 'post', '/_/engage/kudos/{id}/delete', kudos, {}),
    ('kudos reaction', 'post', '/_/engage/react', kudos, {'target_type': 'kudos', 'target_id': '{id}', 'emoji': 'clap'}),
    ('kudos comment', 'post', '/_/engage/comments', kudos, {'target_type': 'kudos', 'target_id': '{id}', 'body': 'Hi'}),
    ('celebration wish', 'post', '/_/engage/comments', beta_employee,
     {'target_type': 'celebration', 'target_id': 'joined:{id}', 'body': 'Welcome'}),
    ('comment delete', 'post', '/_/engage/comments/{id}/delete', comment, {}),
    ('value toggle', 'post', '/_/engage/values/{id}/toggle', value, {}),
    ('survey edit form', 'get', '/_/engage/surveys/{id}/edit', draft_survey, {}),
    ('survey publish', 'post', '/_/engage/surveys/{id}/publish', draft_survey, {}),
    ('survey delete', 'post', '/_/engage/surveys/{id}/delete', draft_survey, {}),
    ('survey close', 'post', '/_/engage/surveys/{id}/close', open_survey, {}),
    ('survey closing-day form', 'get', '/_/engage/surveys/{id}/closing-day', open_survey, {}),
    ('survey extend', 'post', '/_/engage/surveys/{id}/extend', open_survey, {'closes_on': '{today}'}),
    ('survey answer form', 'get', '/_/engage/surveys/{id}/answer', open_survey, {}),
    ('survey results', 'get', '/_/engage/surveys/{id}/results', open_survey, {}),
    ('survey results csv', 'get', '/_/engage/surveys/{id}/results.csv', open_survey, {}),
    ('suggestion reply form', 'get', '/_/engage/suggestions/{id}/reply', suggestion, {}),
    ('suggestion update', 'post', '/_/engage/suggestions/{id}/update', suggestion, {'status': 'reviewed'}),
    ('suggestion reply', 'post', '/_/engage/suggestions/{id}/reply', suggestion, {'reply': 'Thanks, noted'}),
    ('survey edit', 'post', '/_/engage/surveys/{id}/edit', draft_survey,
     {'title': 'Renamed', 'opens_on': '{today}', 'closes_on': '{today}',
      'questions_json': '[{{"kind": "rating", "prompt": "How was it?"}}]'}),
    ('survey answer', 'post', '/_/engage/surveys/{id}/answer', open_survey, {}),
]


def _call(client, method, url, data):
    if method == 'post':
        return client.post(url, data, **htmx())
    return client.get(url, **htmx())


@pytest.mark.parametrize('case', CASES, ids=[c[0] for c in CASES])
def test_ids_of_another_company_are_not_found(group, case):
    name, method, url, maker, data = case
    rid = maker(group)
    target = url.format(id=rid)
    body = {k: v.format(id=rid, today=_today()) for k, v in data.items()}
    for who, client in (('outsider', group.client(group.admin_c)), ('group admin in Alpha', group.client(group.admin_a))):
        r = _call(client, method, target, body)
        assert r.status_code == 404, f'{name}: {who} got {r.status_code}'
    r = _call(group.client(group.admin_b), method, target, body)
    assert r.status_code != 404 and r.status_code < 500, f'{name}: Beta admin got {r.status_code} (control)'


def test_services_treat_foreign_ids_as_not_found(group):
    g = group
    a, k, s = announcement(g), kudos(g), open_survey(g)
    alpha_hr = g.working_in(g.dual, g.alpha)
    for fn, args, message in ((S.ack_announcement, (a,), 'announcement not found'),
                              (S.delete_kudos, (k,), 'kudos not found'),
                              (S.get_survey, (s,), 'survey not found'),
                              (S.results, (s,), 'survey not found'),
                              (S.update_value, (value(g), {'active': False}), 'value not found'),
                              (S.update_suggestion, (suggestion(g), {'status': 'archived'}), 'suggestion not found')):
        with pytest.raises(Exception) as e:
            fn(alpha_hr, *args)
        assert getattr(e.value, 'status', None) == 404 and e.value.message == message, fn.__name__


def test_ids_of_another_company_in_a_body_are_refused(group):
    g = group
    alpha_hr = g.working_in(g.dual, g.alpha)
    with pytest.raises(BadRequestError) as e:
        S.give_kudos(alpha_hr, {'recipientIds': [g.beta_colleague.employee_id], 'message': 'Across the group'})
    assert e.value.message == 'Pick colleagues from your company.'
    with pytest.raises(BadRequestError) as e:
        S.give_kudos(alpha_hr, {'recipientIds': [g.admin_a.employee_id], 'valueId': value(g), 'message': 'With a Beta value'})
    assert e.value.message == 'Pick one of the company values.'
    branch = Branch.objects.create(company=g.beta, code='BHQ', name_en='Beta Head Office')
    with pytest.raises(BadRequestError) as e:
        S.post_announcement(alpha_hr, {'title': 'Hi', 'body': 'x', 'audienceKind': 'branch', 'audienceId': str(branch.id)})
    assert e.value.message == 'pick the branch or department this is for'
    with pytest.raises(BadRequestError) as e:
        S.create_survey(alpha_hr, {'title': 'x', 'audienceKind': 'branch', 'audienceId': str(branch.id),
                                   'opensOn': _today(), 'closesOn': _today(), 'questions': QUESTIONS})
    assert e.value.message == 'pick the branch or department this is for'


def test_feed_and_lists_follow_the_active_company(group):
    g = group
    announcement(g)
    kudos(g)
    open_survey(g)
    suggestion(g)
    c = g.client(g.admin_a)
    feed = c.get('/_/engage/feed', **htmx()).content.decode()
    assert 'Beta news' not in feed and 'Thanks for the help' not in feed
    assert 'Beta pulse' not in c.get('/_/engage/surveys', **htmx()).content.decode()
    assert 'A canteen for Beta' not in c.get('/engage/suggestions').content.decode()
    assert 'Beta spirit' not in c.get('/_/engage/values', **htmx()).content.decode()
    c.post('/_/web/company/switch', {'company_id': str(g.beta.id)}, **htmx())
    feed = c.get('/_/engage/feed', **htmx()).content.decode()
    assert 'Beta news' in feed and 'Thanks for the help' in feed
    assert 'Beta pulse' in c.get('/_/engage/surveys', **htmx()).content.decode()
    assert 'A canteen for Beta' in c.get('/engage/suggestions').content.decode()


def test_a_member_is_reached_and_answers_in_the_company_they_work_in(group):
    g = group
    S.post_announcement(g.admin_b, {'title': 'Beta town hall', 'body': 'Friday'})
    assert NotificationOutbox.objects.filter(user_id=g.dual.id, company=g.beta, template='engage.announcement').exists()
    S.give_kudos(g.admin_b, {'recipientIds': [str(g.dual_beta.id)], 'message': 'Great first week'})
    assert NotificationOutbox.objects.filter(user_id=g.dual.id, company=g.beta, template='engage.kudos').exists()
    sid = open_survey(g)
    assert NotificationOutbox.objects.filter(user_id=g.dual.id, company=g.beta, template='engage.survey_open').exists()

    in_beta = g.working_in(g.dual, g.beta)
    assert [s['id'] for s in S.open_surveys_for(in_beta)] == [sid]
    q = S.get_survey(in_beta, sid)['questions'][0]
    assert S.respond(in_beta, sid, {'answers': [{'questionId': q['id'], 'value': 4}]}) == {'ok': True}
    stored = S.results(g.admin_b, sid)['results']
    assert stored['responses'] == 1 and stored['audience'] == 3

    # Working in Alpha the same person is HR there and sees nothing of Beta.
    in_alpha = g.working_in(g.dual, g.alpha)
    assert S.open_surveys_for(in_alpha) == []
    assert all(i['title'] != 'Beta town hall' for i in S.feed(in_alpha)['items'] if i['type'] == 'announcement')
    acks = S.announcement_acks(g.admin_b, S.post_announcement(g.admin_b, {'title': 'Read me', 'body': 'x',
                                                                           'requiresAck': True})['id'])
    assert 'Dipak at Beta' in [p['name'] for p in acks['people']]
