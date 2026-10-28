"""Pages and HTMX fragments: Time off, the Leave hub (calendar, balances, policies) and the apply form."""
import pytest
from django.test import Client

from tests.support.sql import execute, one, rows
from hajiri.leave.service import ledger_balance
from tests.core.fixtures import PW
from tests.web.factories import hx_triggers, htmx, sign_in, toasts

from .helpers import instance_of


def _client(world, roles, employee_id=None, company_id=None):
    login = world.login_as(roles, employee_id, company_id=company_id)
    c = Client()
    sign_in(c, login.email, PW)
    return c, login


@pytest.fixture
def pages(world):
    world.hr_c, world.hr_login = _client(world, ['hr'])
    # Sita and Ram already have accounts (world); sign those in.
    world.sita_c, world.ram_c = Client(), Client()
    for c, emp in ((world.sita_c, world.demo['sita']), (world.ram_c, world.demo['ram'])):
        sign_in(c, one('SELECT email FROM app_user WHERE employee_id = %s', [emp])['email'], PW)
    return world


def _allocate(w, code='HOME', days=10):
    w.hr.post('/api/v1/leave/adjust', {'employeeId': w.demo['sita'], 'leaveTypeId': w.t(code), 'deltaDays': days,
                                       'reason': 'opening balance', 'kind': 'allocation'})


# ---------------------------------------------------------------- Time off (/me/leave)

def test_time_off_page_shows_balances_and_requests(pages):
    w = pages
    _allocate(w)
    r = w.sita_c.get('/me/leave')
    assert r.status_code == 200
    body = r.content.decode()
    assert 'Time off' in body and 'Request time off' in body
    assert 'Home leave' in body and '10.0' in body
    assert 'No time off requested yet' in body


def test_apply_form_preview_and_submit(pages):
    w = pages
    _allocate(w)
    form = w.sita_c.get('/_/leave/apply', **htmx('hj-drawer-body'))
    assert form.status_code == 200
    html = form.content.decode()
    assert 'data-modal-title="Request time off"' in html and 'Home leave (10.0 days left)' in html
    pv = w.sita_c.get('/_/leave/preview', {'leave_type_id': w.t('HOME'), 'from': '2026-11-02', 'to': '2026-11-03'}, **htmx())
    assert pv.status_code == 200
    assert '2 days will be deducted' in pv.content.decode()
    assert 'Balance after this request: <span class="hj-strong num">8.0</span> of 10.0 days' in pv.content.decode()
    # incomplete inputs show nothing
    assert w.sita_c.get('/_/leave/preview', {'leave_type_id': w.t('HOME')}, **htmx()).content == b''
    r = w.sita_c.post('/_/leave/apply', {'leave_type_id': w.t('HOME'), 'from': '2026-11-02', 'to': '2026-11-03', 'reason': 'family'},
                      **htmx('hj-drawer-body'))
    assert r.status_code == 204
    t = toasts(r)[0]
    assert t['title'] == 'Time off requested' and t['message'] == '2 working day(s) will be deducted'
    trig = hx_triggers(r)
    assert 'hj:refresh-my-leave' in trig and 'hj:close-modal' in trig
    lr = one('SELECT status, days_requested, reason FROM leave_request WHERE employee_id = %s', [w.demo['sita']])
    assert lr['status'] == 'pending' and float(lr['days_requested']) == 2 and lr['reason'] == 'family'
    page = w.sita_c.get('/me/leave').content.decode()
    assert 'Cancel request' in page and 'Home leave' in page


def test_apply_shortfall_shows_suggestion_inline(pages):
    w = pages
    r = w.sita_c.post('/_/leave/apply', {'leave_type_id': w.t('HOME'), 'from': '2026-11-02', 'to': '2026-11-04'}, **htmx())
    assert r.status_code == 422
    html = r.content.decode()
    assert 'Not enough balance' in html
    assert 'You can take 0 paid and 3 unpaid.' in html


def test_apply_other_errors_are_a_toast(pages):
    w = pages
    _allocate(w, 'SICK', 10)
    r = w.sita_c.post('/_/leave/apply', {'leave_type_id': w.t('SICK'), 'from': '2026-11-02', 'to': '2026-11-06'}, **htmx())
    assert r.status_code == 204
    t = toasts(r)[0]
    assert t['tone'] == 'err' and t['title'] == 'Request not sent' and 'certificate' in t['message']


def test_apply_hourly_and_half_day_from_the_form(pages):
    w = pages
    _allocate(w)
    r = w.sita_c.post('/_/leave/apply', {'leave_type_id': w.t('HOME'), 'from': '2026-11-02', 'to': '2026-11-02', 'hourly': 'on',
                                         'from_time': '10:00', 'to_time': '12:00'}, **htmx())
    assert r.status_code == 204, toasts(r)
    lr = one('SELECT hours, days_requested FROM leave_request WHERE employee_id = %s', [w.demo['sita']])
    assert float(lr['hours']) == 2 and float(lr['days_requested']) == 0.25
    r = w.sita_c.post('/_/leave/apply', {'leave_type_id': w.t('HOME'), 'from': '2026-11-03', 'to': '2026-11-03', 'half': 'on',
                                         'half_which': 'second_half'}, **htmx())
    assert r.status_code == 204
    assert one("SELECT half_day FROM leave_request WHERE from_date = '2026-11-03'")['half_day'] == 'second_half'


def test_hr_applies_on_behalf_inline_and_manager_scoping(pages):
    w = pages
    _allocate(w)
    r = w.hr_c.get('/_/leave/apply', {'employee_id': w.demo['sita'], 'inline': '1'}, **htmx())
    assert r.status_code == 200 and 'name="inline" value="1"' in r.content.decode()
    r = w.hr_c.post('/_/leave/apply', {'employee_id': w.demo['sita'], 'inline': '1', 'leave_type_id': w.t('HOME'),
                                       'from': '2026-11-09', 'to': '2026-11-09'}, **htmx())
    assert r.status_code == 200 and 'Send request' in r.content.decode()
    assert toasts(r)[0]['title'] == 'Time off requested'
    # a manager who does not manage Sita cannot open the form for her
    mgr = str(one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'E900', 'Other Manager') RETURNING id", [w.company])['id'])
    c, _ = _client(w, ['manager'], mgr)
    r = c.get('/_/leave/apply', {'employee_id': w.demo['sita']}, **htmx())
    assert r.status_code == 403 and toasts(r)[0]['message'] == 'not allowed to act for this employee'


def test_request_detail_and_cancel(pages):
    w = pages
    _allocate(w)
    rid = w.sita.post('/api/v1/leave/requests', {'leaveTypeId': w.t('HOME'), 'from': '2026-11-02', 'to': '2026-11-02',
                                                 'reason': 'dentist'}).json()['id']
    d = w.sita_c.get(f'/_/leave/requests/{rid}', **htmx('hj-modal-body'))
    assert d.status_code == 200
    html = d.content.decode()
    assert 'data-modal-title="Home leave"' in html and 'dentist' in html and 'Waiting for your manager' in html
    # someone else's employee cannot read it
    c, _ = _client(w, ['employee'], None)
    assert c.get(f'/_/leave/requests/{rid}', **htmx()).status_code == 403
    r = w.sita_c.post(f'/_/leave/requests/{rid}/cancel', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Request cancelled.'
    assert 'hj:refresh-requests' in hx_triggers(r) and 'hj:refresh-my-leave' in hx_triggers(r)
    assert one('SELECT status FROM leave_request WHERE id = %s', [rid])['status'] == 'cancelled'
    again = w.sita_c.post(f'/_/leave/requests/{rid}/cancel', **htmx())
    assert again.status_code == 409 and toasts(again)[0]['message'] == 'request is already cancelled'


def test_request_history_offers_cancel_leave(pages):
    """workflow's Request history shows 'Cancel leave' once leave:cancel exists; HR cancels with reason 'cancelled by HR'."""
    w = pages
    _allocate(w)
    rid = w.sita.post('/api/v1/leave/requests', {'leaveTypeId': w.t('HOME'), 'from': '2026-11-02', 'to': '2026-11-02'}).json()['id']
    w.ram.post(f'/api/v1/workflow/{instance_of(rid)}/approve', {})
    page = w.hr_c.get('/leave/history', {'status': 'approved'}).content.decode()
    assert 'Cancel leave' in page and f'/_/leave/requests/{rid}/cancel' in page
    r = w.hr_c.post(f'/_/leave/requests/{rid}/cancel', {'reason': 'cancelled by HR'}, **htmx())
    assert r.status_code == 204
    assert ledger_balance(w.demo['sita'], w.t('HOME')) == 10
    assert rows("SELECT note FROM leave_ledger WHERE ref_id = %s AND reason = 'cancellation'", [rid])[0]['note'] == 'cancelled by HR'


# ---------------------------------------------------------------- Leave hub

def test_team_calendar_grid_and_list(pages):
    w = pages
    w.sita.post('/api/v1/leave/requests', {'leaveTypeId': w.t('LWP'), 'from': '2026-11-02', 'to': '2026-11-03', 'reason': 'trip'})
    execute("INSERT INTO holiday (company_id, local_date, name_en) VALUES (%s, '2026-11-08', 'Tihar')", [w.company])
    r = w.ram_c.get('/leave', {'y': 2083, 'm': 7})
    assert r.status_code == 200
    html = r.content.decode()
    assert 'Kartik 2083' in html and 'Sita' in html and 'Sita Sharma, LWP, pending' in html and 'Tihar' in html
    lst = w.ram_c.get('/leave', {'y': 2083, 'm': 7, 'view': 'list'}).content.decode()
    assert 'Public holiday: Tihar' in lst and 'Sita Sharma' in lst and 'E001' in lst
    empty = w.ram_c.get('/leave', {'y': 2083, 'm': 1, 'view': 'list'}).content.decode()
    assert 'Everyone is in this month' in empty
    # employees do not see the Leave hub
    c, _ = _client(w, ['employee'])
    assert c.get('/leave', **htmx()).status_code == 403


def test_balances_page_adjust_and_ledger(pages):
    w = pages
    r = w.hr_c.get('/leave/balances')
    assert r.status_code == 200
    assert 'Leave adjustments' in r.content.decode() and 'No employee selected' in r.content.decode()
    r = w.hr_c.post('/_/leave/adjust', {'employee_id': w.demo['sita'], 'leave_type_id': w.t('HOME'), 'delta_days': '2.5',
                                        'kind': 'allocation', 'reason': 'opening balance', 'effective_date': '2026-10-01'}, **htmx())
    assert r.status_code == 204
    assert toasts(r)[0]['message'] == 'Adjustment posted. New balance is 2.50 days.'
    assert 'hj:refresh-leave-balances' in hx_triggers(r)
    page = w.hr_c.get('/leave/balances', {'employee_id': w.demo['sita']}).content.decode()
    assert '2.50' in page and 'opening balance' in page and '+2.50' in page and '15 Ashwin 2083' in page
    part = w.hr_c.get('/leave/balances', {'employee_id': w.demo['sita']}, **htmx('leave-balances'))
    assert part.status_code == 200 and '<html' not in part.content.decode() and 'id="leave-balances"' in part.content.decode()
    bad = w.hr_c.post('/_/leave/adjust', {'employee_id': w.demo['sita'], 'leave_type_id': w.t('HOME'), 'delta_days': '0',
                                          'reason': 'opening'}, **htmx())
    assert bad.status_code == 400 and 'deltaDays cannot be 0' in toasts(bad)[0]['message']
    c, _ = _client(w, ['manager'], None)
    assert c.post('/_/leave/adjust', {}, **htmx()).status_code == 403


def test_policies_page_create_edit_and_order(pages):
    w = pages
    r = w.hr_c.get('/leave/policies')
    assert r.status_code == 200
    html = r.content.decode()
    assert 'Leave policies' in html and 'Statutory' in html and 'Sec 44: 12/yr, cap 45, certificate over 3 days' in html
    assert '1 day per 20 worked' in html and '6 days a year' in html and 'Allocated manually' in html
    form = w.hr_c.get('/_/leave/types/new', **htmx('hj-drawer-body'))
    assert form.status_code == 200 and 'New leave type' in form.content.decode()
    r = w.hr_c.post('/_/leave/types/new', {'code': 'study', 'nameEn': 'Study leave', 'genderScope': 'all', 'paid': 'on',
                                           'accrualBasis': 'annual', 'accrualDays': '3', 'negativeBalanceLimit': '0',
                                           'minServiceDays': '0', 'allowHalfDay': 'on'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Leave policy saved.'
    st = one("SELECT * FROM leave_type WHERE company_id = %s AND code = 'STUDY'", [w.company])
    assert st['name_en'] == 'Study leave' and float(st['accrual_days']) == 3 and st['sort_order'] == 100 and not st['statutory']
    edit = w.hr_c.get(f"/_/leave/types/{st['id']}/edit", **htmx()).content.decode()
    assert 'Edit Study leave' in edit
    r = w.hr_c.post(f"/_/leave/types/{st['id']}/edit", {'nameEn': 'Study', 'accrualBasis': 'annual', 'accrualDays': '4', 'paid': 'on',
                                                        'genderScope': 'all', 'sandwichRule': 'on', 'clubWithId': w.t('HOME')}, **htmx())
    assert r.status_code == 204
    st = one('SELECT * FROM leave_type WHERE id = %s', [st['id']])
    assert st['name_en'] == 'Study' and float(st['accrual_days']) == 4 and st['sandwich_rule'] and str(st['club_with_id']) == w.t('HOME')
    # statutory types: the form shows the lock and never sends entitlement fields
    home_form = w.hr_c.get(f"/_/leave/types/{w.t('HOME')}/edit", **htmx()).content.decode()
    assert 'Entitlement fields cannot be changed.' in home_form
    r = w.hr_c.post(f"/_/leave/types/{w.t('HOME')}/edit", {'nameEn': 'Home leave', 'paid': 'on', 'allowHalfDay': 'on', 'allowHourly': 'on',
                                                          'carryForward': 'on', 'encashable': 'on', 'maxBalance': '200'}, **htmx())
    assert r.status_code == 204, toasts(r)
    assert float(one('SELECT max_balance FROM leave_type WHERE id = %s', [w.t('HOME')])['max_balance']) == 90
    # order
    order = [str(x['id']) for x in rows('SELECT id FROM leave_type WHERE company_id = %s ORDER BY sort_order, code', [w.company])]
    r = w.hr_c.post(f'/_/leave/types/{order[1]}/move', {'dir': 'up'}, **htmx())
    assert r.status_code == 204 and 'hj:refresh-leave-types' in hx_triggers(r)
    new_order = [str(x['id']) for x in rows('SELECT id FROM leave_type WHERE company_id = %s ORDER BY sort_order, code', [w.company])]
    assert new_order[0] == order[1] and new_order[1] == order[0]
    # specialist attendance admin may edit policies but not reorder (HR only, as the TS route)
    c, _ = _client(w, ['attendance_admin'])
    assert c.get('/leave/policies').status_code == 200
    assert c.post(f'/_/leave/types/{order[1]}/move', {'dir': 'up'}, **htmx()).status_code == 403
    emp_c, _ = _client(w, ['employee'])
    assert emp_c.post('/_/leave/types/new', {}, **htmx()).status_code == 403


def test_legacy_addresses_redirect(pages):
    w = pages
    assert w.hr_c.get('/manager/calendar').headers['Location'] == '/leave'
    assert w.hr_c.get('/hr/leave-adjust').headers['Location'] == '/leave/balances'
    assert w.hr_c.get('/hr/leave-types?x=1').headers['Location'] == '/leave/policies?x=1'


def test_navigation_tabs(pages):
    w = pages
    html = w.hr_c.get('/leave').content.decode()
    for label in ('Calendar', 'Balances', 'Policies', 'Request history'):
        assert label in html
    mine = w.sita_c.get('/me/leave').content.decode()
    assert 'Time off' in mine


def test_apply_form_pickers_mark_every_company_holiday(pages):
    """LeaveApplyForm marked the days from GET /shifts/calendar: every company holiday in the window, whatever its
    gender scope or optional flag, names joined with ', '."""
    from hajiri.web import format as fmt
    w = pages
    day = fmt.add_days(fmt.today_npt(), 10)
    execute("INSERT INTO holiday (company_id, local_date, name_en, gender_scope) VALUES (%s, %s, 'Teej', 'female')", [w.company, day])
    execute("INSERT INTO holiday (company_id, local_date, name_en, optional) VALUES (%s, %s, 'Optional day', true)", [w.company, day])
    far = fmt.add_days(fmt.today_npt(), 200)
    execute("INSERT INTO holiday (company_id, local_date, name_en) VALUES (%s, %s, 'Too far')", [w.company, far])
    r = w.ram_c.get('/_/leave/apply', **htmx('hj-drawer-body'))
    assert r.status_code == 200
    assert r.context['holidays'][day] == 'Optional day, Teej'
    assert far not in r.context['holidays']
    html = r.content.decode()
    assert 'Must be after the start time' in html and 'Only for a single day' in html
