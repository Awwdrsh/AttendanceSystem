"""
Time tracking in a group of companies: every page, partial and JSON endpoint works in the ACTIVE company only. Records
of a sister company are not found (404) until the person switches to it, people who work in two companies log time on
the employee record of the company they are working in, and lists and reports never mix companies.
"""
from __future__ import annotations

import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.db.models import TimeEntry, Timesheet
from hajiri.timesheets import services as ts
from tests.multicompany.conftest import world  # noqa: F401  (the group fixture)
from tests.web.factories import bearer, htmx

from .helpers import add_days, today_npt


def _week() -> tuple[str, str]:
    week = ts.week_start_of(add_days(today_npt(), -14))
    return week, add_days(week, 1)


@pytest.fixture
def beta(world):  # noqa: F811
    """Beta's catalogue and one submitted week of Beta's admin, made by Beta's own people."""
    _, admin_b = world.session(world.admin_b)
    week, mon = _week()
    client = ts.create_client(admin_b, {'name': 'Beta Client'})['id']
    project = ts.create_project(admin_b, {'code': 'BETA-1', 'name': 'Beta project', 'clientId': client})
    activity = ts.create_activity(admin_b, {'name': 'Beta activity', 'projectId': project})['id']
    entry = ts.add_entry(admin_b, {'date': mon, 'durationMin': 60, 'projectId': project})['id']
    sheet = ts.submit_timesheet(admin_b, {'weekStart': week})
    return {'client': client, 'project': project, 'activity': activity, 'entry': entry, 'timesheet': sheet,
            'week': week, 'mon': mon, 'admin_b': admin_b}


PAGES = [
    ('get', '/_/timesheets/projects/{project}/edit'),
    ('post', '/_/timesheets/projects/{project}/edit'),
    ('post', '/_/timesheets/clients/{client}/archive'),
    ('post', '/_/timesheets/activities/{activity}/archive'),
    ('get', '/_/timesheets/{timesheet}'),
    ('post', '/_/timesheets/{timesheet}/decide'),
]


@pytest.mark.parametrize('method,url', PAGES)
def test_sister_company_records_are_not_found_on_pages(world, beta, method, url):  # noqa: F811
    path = url.format(**beta)
    data = {'decision': 'approve', 'name': 'x', 'code': 'X', 'archived': 'true'} if method == 'post' else None
    outsider = world.signed_in(world.admin_c)
    group_admin_in_alpha = world.signed_in(world.admin_a, world.alpha)
    for c in (outsider, group_admin_in_alpha):
        r = getattr(c, method)(path, data, **htmx()) if data else getattr(c, method)(path, **htmx())
        assert r.status_code == 404, (path, r.status_code)
    owner = world.signed_in(world.admin_b)
    if method == 'get':
        assert owner.get(path, **htmx()).status_code == 200


def test_sister_company_records_are_not_found_in_the_api(world, beta):  # noqa: F811
    c = Client()
    token_a, _ = world.session(world.admin_a, world.alpha)
    for h in (bearer(token_a), bearer(world.session(world.admin_c)[0])):
        assert c.get(f"/api/v1/time/timesheets/{beta['timesheet']}", **h).status_code == 404
        assert c.patch(f"/api/v1/time/projects/{beta['project']}", {'name': 'x'}, content_type='application/json',
                       **h).status_code == 404
        assert c.patch(f"/api/v1/time/clients/{beta['client']}", {'name': 'x'}, content_type='application/json',
                       **h).status_code == 404
        assert c.patch(f"/api/v1/time/activities/{beta['activity']}", {'name': 'x'}, content_type='application/json',
                       **h).status_code == 404
        assert c.get('/api/v1/time/entries', {'from': beta['week'], 'to': beta['mon'],
                                              'employeeId': str(Timesheet.objects.get(pk=beta['timesheet']).employee_id)},
                     **h).status_code == 404
    owner = bearer(world.session(world.admin_b)[0])
    assert c.get(f"/api/v1/time/timesheets/{beta['timesheet']}", **owner).status_code == 200


def test_the_group_admin_reaches_beta_after_switching(world, beta):  # noqa: F811
    c = world.signed_in(world.admin_a, world.beta)
    assert c.get(f"/_/timesheets/projects/{beta['project']}/edit", **htmx()).status_code == 200
    body = c.get(f"/_/timesheets/{beta['timesheet']}", **htmx()).content.decode()
    assert 'Bimal Admin' in body


def test_lists_and_reports_show_only_the_active_company(world, beta):  # noqa: F811
    _, solo = world.session(world.solo)
    ts.create_project(solo, {'code': 'ALPHA-1', 'name': 'Alpha project'})
    ts.add_entry(solo, {'date': beta['mon'], 'durationMin': 30})
    _, admin_a = world.session(world.admin_a, world.alpha)
    assert [p['code'] for p in ts.list_projects(admin_a, True)] == ['ALPHA-1']
    assert [c['name'] for c in ts.list_clients(admin_a, True)] == []
    assert ts.list_activities(admin_a, {'all': '1'}) == []
    assert ts.list_timesheets(admin_a, {'scope': 'all'}) == []
    q = {'from': beta['week'], 'to': add_days(beta['week'], 6), 'groupBy': 'employee', 'scope': 'all'}
    assert [r['label'] for r in ts.report_summary(admin_a, q)['rows']] == ['Sita Solo']
    assert [p['name_en'] for p in ts.team_week(admin_a, {'weekStart': beta['week']})['people']] == [
        'Asha Admin', 'Dipak Dual', 'Kiran Group Exec', 'Sita Solo']
    # The same person working in Beta sees Beta's time.
    _, admin_a_beta = world.session(world.admin_a, world.beta)
    assert [p['code'] for p in ts.list_projects(admin_a_beta, True)] == ['BETA-1']
    assert [r['label'] for r in ts.report_summary(admin_a_beta, q)['rows']] == ['Bimal Admin']
    assert [t['id'] for t in ts.list_timesheets(admin_a_beta, {'scope': 'all'})] == [beta['timesheet']]
    # The pages agree.
    page = world.signed_in(world.admin_a, world.alpha).get('/attendance/projects').content.decode()
    assert 'Alpha project' in page and 'Beta project' not in page


def test_group_hr_reviews_each_company_in_turn(world, beta):  # noqa: F811
    """group_hr is HR in every company of the group, one active company at a time."""
    _, ghr_beta = world.session(world.ghr, world.beta)
    assert [t['id'] for t in ts.list_timesheets(ghr_beta, {'scope': 'all'})] == [beta['timesheet']]
    _, ghr_alpha = world.session(world.ghr, world.alpha)
    assert ts.list_timesheets(ghr_alpha, {'scope': 'all'}) == []
    with pytest.raises(Exception) as e:
        ts.timesheet_detail(ghr_alpha, beta['timesheet'])
    assert getattr(e.value, 'status', None) == 404


def test_a_person_in_two_companies_logs_time_where_they_work(world, beta):  # noqa: F811
    token, dual_alpha = world.session(world.dual)
    dual_beta = auth.switch_company(dual_alpha, token, str(world.beta.id))
    assert dual_beta.employee_id == str(world.dual_beta_employee.id)
    run = ts.start_timer(dual_beta, {'projectId': beta['project']})
    row = TimeEntry.objects.get(pk=run['id'])
    assert (str(row.company_id), str(row.employee_id)) == (str(world.beta.id), str(world.dual_beta_employee.id))
    ts.stop_timer(dual_beta)
    # Back in Alpha: Beta's project cannot be logged to and the Beta entry is not theirs here.
    dual_alpha = auth.switch_company(dual_beta, token, str(world.alpha.id))
    assert ts.timer_state(dual_alpha)['last'] is None
    with pytest.raises(Exception) as e:
        ts.add_entry(dual_alpha, {'date': beta['mon'], 'durationMin': 15, 'projectId': beta['project']})
    assert str(e.value) == 'Project not found.'
    with pytest.raises(Exception) as e:
        ts.update_entry(dual_alpha, run['id'], {'note': 'x'})
    assert getattr(e.value, 'status', None) == 404
    # Through the pages: the Beta entry's edit form is 404 while working in Alpha.
    c = Client()
    c.cookies[auth.SESSION_COOKIE] = token
    assert c.get(f"/_/timesheets/entries/{run['id']}/edit", **htmx()).status_code == 404


def test_ids_from_a_sister_company_are_refused_inside_bodies(world, beta):  # noqa: F811
    _, solo = world.session(world.solo)
    beta_employee = str(Timesheet.objects.get(pk=beta['timesheet']).employee_id)
    with pytest.raises(Exception) as e:
        ts.create_project(solo, {'code': 'MIX', 'name': 'Mixed', 'allMembers': False, 'memberIds': [beta_employee]})
    assert str(e.value) == 'Some members are not employees of this company.'
    with pytest.raises(Exception) as e:
        ts.create_project(solo, {'code': 'MIX', 'name': 'Mixed', 'clientId': beta['client']})
    assert str(e.value) == 'Client not found.'
    with pytest.raises(Exception) as e:
        ts.create_activity(solo, {'name': 'x', 'projectId': beta['project']})
    assert str(e.value) == 'Project not found.'
    with pytest.raises(Exception) as e:
        ts.add_entry(solo, {'date': beta['mon'], 'durationMin': 15, 'activityId': beta['activity']})
    assert str(e.value) == 'That activity is not available without a project.'
    with pytest.raises(Exception) as e:
        ts.report_summary(solo, {'from': beta['week'], 'to': beta['mon'], 'employeeId': beta_employee})
    assert getattr(e.value, 'status', None) == 404


def test_the_project_form_lists_only_the_active_company_people(world, beta):  # noqa: F811
    body = world.signed_in(world.admin_a, world.alpha).get('/_/timesheets/projects/new', **htmx()).content.decode()
    assert 'Sita Solo' in body and 'Bimal Admin' not in body and 'Dipak at Beta' not in body


def test_daily_jobs_cover_every_company(world, beta):  # noqa: F811
    from datetime import datetime, timedelta, timezone
    admin_b = beta['admin_b']
    _, solo = world.session(world.solo)
    ts.save_settings(world.session(world.admin_a, world.alpha)[1], {'autoStopHours': 2})
    a = ts.start_timer(solo, {})
    b = ts.start_timer(admin_b, {})
    TimeEntry.objects.filter(pk__in=[a['id'], b['id']]).update(started_at=datetime.now(timezone.utc) - timedelta(hours=13))
    assert ts.auto_stop_stale_timers() == 2
    stopped = {str(e.id): e.duration_min for e in TimeEntry.objects.filter(pk__in=[a['id'], b['id']])}
    assert stopped == {a['id']: 120, b['id']: 720}           # Alpha stops at 2 h, Beta at the default 12 h


def test_sister_company_writes_and_reads_by_id_are_not_found(world, beta):  # noqa: F811
    """Deleting a Beta entry, withdrawing a Beta week and reading a Beta person's week are 404 from Alpha, on the
    pages and in the API; Beta's own admin can still withdraw."""
    beta_employee = str(Timesheet.objects.get(pk=beta['timesheet']).employee_id)
    group_admin_in_alpha = world.signed_in(world.admin_a, world.alpha)
    for c in (world.signed_in(world.admin_c), group_admin_in_alpha):
        assert c.post(f"/_/timesheets/entries/{beta['entry']}/delete", **htmx()).status_code == 404
        assert c.post(f"/_/timesheets/{beta['timesheet']}/withdraw", **htmx()).status_code == 404
    token, _ = world.session(world.admin_a, world.alpha)
    h = bearer(token)
    c = Client()
    assert c.delete(f"/api/v1/time/entries/{beta['entry']}", **h).status_code == 404
    assert c.post(f"/api/v1/time/timesheets/{beta['timesheet']}/withdraw", **h).status_code == 404
    assert c.get('/api/v1/time/week', {'employeeId': beta_employee}, **h).status_code == 404
    assert c.get('/api/v1/time/timesheets', {'employeeId': beta_employee}, **h).status_code == 404
    assert TimeEntry.objects.filter(pk=beta['entry']).exists()
    assert Timesheet.objects.get(pk=beta['timesheet']).status == Timesheet.Status.SUBMITTED
    # A Beta project id in the activity filter only narrows to Alpha's company-wide activities.
    _, admin_a = world.session(world.admin_a, world.alpha)
    ts.create_activity(admin_a, {'name': 'Alpha meetings'})
    assert [a['name'] for a in ts.list_activities(admin_a, {'projectId': beta['project']})] == ['Alpha meetings']
    ts.withdraw_timesheet(beta['admin_b'], beta['timesheet'])
    assert Timesheet.objects.get(pk=beta['timesheet']).status == Timesheet.Status.CANCELLED


def test_friday_reminder_reaches_a_member_working_in_a_sister_company(world, beta):  # noqa: F811
    """The reminder for a Beta employee record held through a membership goes to that member, in Beta."""
    from datetime import datetime, time, timezone

    from hajiri.core.clock import NPT
    from hajiri.db.models import NotificationOutbox
    token, dual_alpha = world.session(world.dual)
    dual_beta = auth.switch_company(dual_alpha, token, str(world.beta.id))
    ts.add_entry(dual_beta, {'date': beta['mon'], 'durationMin': 30, 'projectId': beta['project']})
    friday = datetime.combine(ts._d(add_days(beta['week'], 5)), time(10), tzinfo=NPT).astimezone(timezone.utc)
    assert ts.friday_timesheet_reminders(friday) >= 1
    sent = NotificationOutbox.objects.filter(template='timesheet.reminder', user_id=world.dual.id)
    assert {(str(n.company_id), str(n.employee_id)) for n in sent} == {(str(world.beta.id), str(world.dual_beta_employee.id))}
    # Beta's admin submitted that week: no reminder. And a second run sends nothing new.
    submitted_by = Timesheet.objects.get(pk=beta['timesheet']).employee_id
    assert not NotificationOutbox.objects.filter(template='timesheet.reminder', employee_id=submitted_by).exists()
    assert ts.friday_timesheet_reminders(friday) == 0
