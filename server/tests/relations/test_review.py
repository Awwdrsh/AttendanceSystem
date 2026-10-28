"""Review checks of the idiomatic relations slice: list pages run a fixed number of queries however many rows they
show, an impossible hearing time is a 400 (not a crash), and the employee side of KYE and conduct works only in the
company the person is working in."""
from django.db import connection
from django.test.utils import CaptureQueriesContext

from hajiri.db.models import KyeResponse
from hajiri.relations import services as S
from tests.web.factories import htmx, make_user, toasts

from .conftest import client_for
from .test_isolation import client_in, grp  # noqa: F401  (the group fixture)

LISTS = ('/relations', '/relations/disciplinary', '/relations/assignments?status=all', '/relations/kye',
         '/relations/devices?status=all', '/relations/milestones?days=366', '/me/help/grievances')


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
    pages = {url: hr for url in LISTS[:-1]} | {LISTS[-1]: member, f'/_/relations/kye/{camp}': hr}
    before = {url: _count(c, url) for url, c in pages.items()}
    camp2 = _add_rows(w, 5, 100)
    assert KyeResponse.objects.filter(campaign_id=camp2).count() > KyeResponse.objects.filter(campaign_id=camp).count()
    pages[f'/_/relations/kye/{camp2}'] = pages.pop(f'/_/relations/kye/{camp}')
    before[f'/_/relations/kye/{camp2}'] = before.pop(f'/_/relations/kye/{camp}')
    after = {url: _count(c, url) for url, c in pages.items()}
    assert after == before


def test_an_impossible_hearing_time_is_refused_not_a_server_error(world):
    w = world
    S.set_committee(w.hr, {'userIds': [w.member.id]})
    gid = S.file_grievance(w.sita, {'category': 'other', 'subject': 'Parking', 'description': 'No space'})['id']
    m = client_for(w.member)
    for date_, time_ in (('2026-02-30', '10:00'), ('2026-11-01', '25:00')):
        r = m.post(f'/_/relations/grievances/{gid}/update', {'action': 'hearing', 'date': date_, 'time': time_},
                   **htmx('hj-drawer-body'))
        assert r.status_code == 400 and toasts(r)[0]['tone'] == 'err', (date_, time_)
    try:
        S.update_grievance(w.member, gid, {'hearingAt': '2026-02-30T10:00:00+05:45'})
    except Exception as e:  # noqa: BLE001
        assert getattr(e, 'status', None) == 400 and str(e) == 'invalid value format'
    else:
        raise AssertionError('accepted 30 February')


def test_employee_answers_kye_and_cases_only_in_the_active_company(grp):  # noqa: F811
    w = grp
    # Dipak is an employee of Beta by membership: the Beta campaign reached that record.
    assert KyeResponse.objects.filter(campaign_id=w.kye, employee_id=w.dual_beta.id).exists()
    case = S.open_case(w.hr_b, {'employeeId': str(w.dual_beta.id), 'category': 'conduct', 'incidentDate': '2026-10-01',
                                'incident': 'Beta incident'})['id']
    S.show_cause(w.hr_b, case, {'text': 'Explain', 'responseDue': '2099-01-01'})
    at_home = client_in(w.dual)
    assert at_home.post(f'/_/relations/kye/{w.kye}/respond', {'status': 'confirmed'}, **htmx()).status_code == 404
    assert at_home.post(f'/_/relations/cases/{case}/respond', {'response': 'Sorry'}, **htmx()).status_code == 404
    assert 'Explain' not in at_home.get('/me/profile/conduct').content.decode()
    in_beta = client_in(w.dual, w.beta)
    r = in_beta.post(f'/_/relations/kye/{w.kye}/respond', {'status': 'confirmed'}, **htmx())
    assert r.status_code == 204, r.content
    assert KyeResponse.objects.get(campaign_id=w.kye, employee_id=w.dual_beta.id).status == KyeResponse.Status.CONFIRMED
    assert in_beta.post(f'/_/relations/cases/{case}/respond', {'response': 'Sorry'}, **htmx()).status_code == 204
    # A Beta employee's own device cannot be removed from Alpha either.
    assert client_in(w.admin_a).post(f'/_/relations/devices/mine/{w.device}/remove', {}, **htmx()).status_code == 404
