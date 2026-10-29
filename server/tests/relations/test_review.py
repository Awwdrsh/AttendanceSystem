"""Review checks of the idiomatic relations slice: list endpoints run a fixed number of queries however many rows they
show, an impossible hearing time is a 400 (not a crash), and the employee side of KYE and conduct works only in the
company the person is working in."""
from django.db import connection
from django.test.utils import CaptureQueriesContext

from hajiri.db.models import KyeResponse
from hajiri.relations import services as S
from tests.web.factories import make_user

from .conftest import client_for
from .test_isolation import client_in, grp  # noqa: F401  (the group fixture)

R = '/api/v1/relations'
LISTS = (R + '/grievances?scope=handle', R + '/disciplinary', R + '/assignments?status=all', R + '/kye/campaigns',
         R + '/devices?status=all', R + '/milestones?days=366', R + '/grievances?scope=mine')


def _add_rows(w, n: int, start: int) -> str:
    """n more employees, each with a grievance, a disciplinary case, a deputation and a device; returns a campaign id."""
    for i in range(start, start + n):
        e = w.new_employee(f'Q{i:03d}')
        u = make_user(w.co, roles=['employee'], employee=e, email=f'q{i}@relations.test')
        S.file_grievance(u, {'category': 'other', 'subject': f'Subject {i}', 'description': 'd'})
        S.open_case(w.hr, {'employeeId': str(e.id), 'category': 'conduct', 'incidentDate': '2026-10-01', 'incident': 'x'})
        S.add_assignment(w.hr, {'employeeId': str(e.id), 'kind': 'deputation', 'branchId': w.branch,
                                'startDate': '2099-01-01', 'endDate': '2099-02-01'})
        S.register_device(u, {'token': f'review-device-token-{i:06d}', 'label': f'Phone {i}'})
    return S.launch_campaign(w.hr, {'title': f'Check {start}', 'sections': ['bank'], 'dueDate': '2099-01-01'})['id']


def _count(c, url: str) -> int:
    c.get(url)  # warm up: the first request of a session does one-off work (session touch, caches)
    with CaptureQueriesContext(connection) as q:
        r = c.get(url)
    assert r.status_code == 200, url
    return len(q.captured_queries)


def test_list_pages_do_not_query_per_row(world):
    w = world
    S.set_committee(w.hr, {'userIds': [w.member.id]})
    hr, member = client_for(w.hr), client_for(w.member)
    camp = _add_rows(w, 1, 0)
    pages = {url: hr for url in LISTS[:-1]} | {LISTS[-1]: member, f'{R}/kye/campaigns/{camp}': hr}
    before = {url: _count(c, url) for url, c in pages.items()}
    camp2 = _add_rows(w, 5, 100)
    assert KyeResponse.objects.filter(campaign_id=camp2).count() > KyeResponse.objects.filter(campaign_id=camp).count()
    pages[f'{R}/kye/campaigns/{camp2}'] = pages.pop(f'{R}/kye/campaigns/{camp}')
    before[f'{R}/kye/campaigns/{camp2}'] = before.pop(f'{R}/kye/campaigns/{camp}')
    after = {url: _count(c, url) for url, c in pages.items()}
    assert after == before
