"""
The /api/v1/engage JSON API in a group of companies (the same world as test_isolation.py): every endpoint taking an id
answers 404 for a record of Beta to Gamma's admin (outside the group) and to the group admin while they work in Alpha;
Beta's admin is the control. Lists show only the active company, and switching changes them.
"""
from __future__ import annotations

import json

import pytest

from .test_isolation import (announcement, beta_employee, comment, draft_survey, group, kudos, open_survey,  # noqa: F401
                             suggestion, value)

A = '/api/v1/engage'

CASES = [
    # (name, method, path, maker, body)
    ('announcement edit', 'patch', '/announcements/{id}', announcement, {'title': 'x'}),
    ('announcement delete', 'delete', '/announcements/{id}', announcement, None),
    ('announcement ack', 'post', '/announcements/{id}/ack', announcement, {}),
    ('announcement acks', 'get', '/announcements/{id}/acks', announcement, None),
    ('announcement reaction', 'post', '/reactions', announcement, {'targetType': 'announcement', 'targetId': '{id}', 'emoji': 'like'}),
    ('announcement comments', 'get', '/comments?targetType=announcement&targetId={id}', announcement, None),
    ('kudos delete', 'delete', '/kudos/{id}', kudos, None),
    ('kudos reaction', 'post', '/reactions', kudos, {'targetType': 'kudos', 'targetId': '{id}', 'emoji': 'clap'}),
    ('kudos comment', 'post', '/comments', kudos, {'targetType': 'kudos', 'targetId': '{id}', 'body': 'Hi'}),
    ('celebration wish', 'post', '/comments', beta_employee, {'targetType': 'celebration', 'targetId': 'joined:{id}', 'body': 'Hi'}),
    ('comment delete', 'delete', '/comments/{id}', comment, None),
    ('value edit', 'patch', '/values/{id}', value, {'active': False}),
    ('survey read', 'get', '/surveys/{id}', open_survey, None),
    ('survey edit', 'put', '/surveys/{id}', open_survey, {'closesOn': '2099-01-01'}),
    ('survey delete', 'delete', '/surveys/{id}', draft_survey, None),
    ('survey publish', 'post', '/surveys/{id}/publish', draft_survey, {}),
    ('survey close', 'post', '/surveys/{id}/close', open_survey, {}),
    ('survey results', 'get', '/surveys/{id}/results', open_survey, None),
    ('survey results csv', 'get', '/surveys/{id}/results.csv', open_survey, None),
    ('suggestion update', 'patch', '/suggestions/{id}', suggestion, {'status': 'reviewed'}),
]


def _call(client, method, path, body):
    if body is None:
        return getattr(client, method)(A + path)
    return getattr(client, method)(A + path, json.dumps(body), content_type='application/json')


def _fill(v, rid):
    if isinstance(v, dict):
        return {k: _fill(x, rid) for k, x in v.items()}
    return v.replace('{id}', rid) if isinstance(v, str) else v


@pytest.mark.parametrize('name,method,path,maker,body', CASES, ids=[c[0] for c in CASES])
def test_records_of_another_company_are_not_found(group, name, method, path, maker, body):
    g = group
    rid = maker(g)
    url, data = path.replace('{id}', rid), _fill(body, rid)
    for who in (g.client(g.admin_c), g.client(g.admin_a)):
        r = _call(who, method, url, data)
        assert r.status_code == 404, (name, r.status_code, r.content)
    r = _call(g.client(g.admin_b), method, url, data)
    assert r.status_code != 404, (name, r.content)


def test_lists_follow_the_active_company(group):
    g = group
    aid = announcement(g)
    kid = kudos(g)
    suggestion(g)
    alpha = g.client(g.admin_a)
    assert aid not in [a['id'] for a in alpha.get(f'{A}/announcements').json()['announcements']]
    assert kid not in [i['id'] for i in alpha.get(f'{A}/feed').json()['items']]
    assert alpha.get(f'{A}/suggestions').json() == {'suggestions': []}
    assert g.beta_colleague.employee_id not in [c['id'] for c in alpha.get(f'{A}/colleagues').json()['colleagues']]
    # The group admin switches to Beta and sees Beta's.
    beta = g.client(g.admin_a, g.beta)
    assert aid in [a['id'] for a in beta.get(f'{A}/announcements').json()['announcements']]
    assert kid in [i['id'] for i in beta.get(f'{A}/feed').json()['items']]
    assert len(beta.get(f'{A}/suggestions').json()['suggestions']) == 1
    assert g.beta_colleague.employee_id in [c['id'] for c in beta.get(f'{A}/colleagues').json()['colleagues']]


def test_ids_of_another_company_in_a_body_are_refused(group):
    g = group
    alpha = g.client(g.admin_a)
    r = alpha.post(f'{A}/kudos', json.dumps({'recipientIds': [g.beta_colleague.employee_id], 'message': 'Thanks a lot'}),
                   content_type='application/json')
    assert r.status_code == 400 and r.json() == {'error': 'Pick colleagues from your company.'}
    vid = value(g)
    r = alpha.post(f'{A}/kudos', json.dumps({'recipientIds': [str(g.dual.employee_id)], 'valueId': vid, 'message': 'Thanks'}),
                   content_type='application/json')
    assert r.status_code == 400 and r.json() == {'error': 'Pick one of the company values.'}


def test_member_of_beta_answers_betas_survey_while_working_there(group):
    g = group
    sid = open_survey(g)
    home = g.client(g.dual)
    assert home.get(f'{A}/surveys/{sid}').status_code == 404
    there = g.client(g.dual, g.beta)
    s = there.get(f'{A}/surveys/{sid}')
    assert s.status_code == 200
    q = s.json()['questions'][0]
    r = there.post(f'{A}/surveys/{sid}/respond', json.dumps({'answers': [{'questionId': q['id'], 'value': 4}]}),
                   content_type='application/json')
    assert r.status_code == 201
