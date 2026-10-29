"""
Field-by-field checks of the leave JSON API against what the React pages send and read (LeavePage / LeaveApplyForm,
LeaveAdjustPage, LeaveTypesPage, TeamCalendarPage, RequestsPage, EmployeeDetailPage) and what the TS routes
(modules/leave/routes.ts at 65a9079) returned: the exact bodies the pages build, hourly and half-day leave, event leave,
the balance explanation text, the leave-type edit drawer and HR cancelling on someone's behalf.
"""
from __future__ import annotations

from tests.leave.test_api_leave import ISO, L, _home


# ---------------------------------------------------------------- LeaveApplyForm: preview and request

def test_hourly_preview_request_and_rows(world):
    _home(world)
    home = world.t('HOME')
    assert world.hr.patch(f'{L}/types/{home}', {'allowHourly': True}).status_code == 200
    q = {'employeeId': world.demo['sita'], 'leaveTypeId': home, 'from': '2026-09-21', 'to': '2026-09-21',
         'fromTime': '10:00', 'toTime': '12:00'}
    p = world.sita.get(f'{L}/preview', q).json()
    # the form reads hours, days, explanation, sandwichApplied, bridge, balanceAfter and available as numbers
    assert p['hours'] == 2 and p['days'] == 0.25 and p['minutes'] == 120 and p['bridge'] is None
    assert (p['balance'], p['available'], p['balanceAfter']) == (10, 10, 9.75)
    assert p['sandwichApplied'] is False and isinstance(p['explanation'], str)
    one_sided = world.sita.get(f'{L}/preview', {k: v for k, v in q.items() if k != 'toTime'})
    assert one_sided.status_code == 400 and one_sided.json()['error'] == 'give both a start and an end time for hourly leave'

    made = world.sita.post(f'{L}/requests', {**q, 'reason': 'dentist', 'attachmentKey': 'REF-1'})
    assert made.status_code == 201, made.json()
    assert made.json()['days'] == 0.25 and made.json()['teamOverlap'] == []

    row = world.sita.get(f'{L}/requests', {'scope': 'own'}).json()['requests'][0]
    assert (row['from_time'], row['to_time'], row['hours'], row['days_requested']) == ('10:00', '12:00', '2.00', '0.25')
    assert row['attachment_key'] == 'REF-1' and row['bridge_days'] == '0.00' and row['bridge_type_id'] is None
    assert row['created_at'].endswith('Z') and row['paid'] is True and row['name_en'] and row['emp_code']

    off = world.hr.get(f'{L}/team-calendar', {'from': '2026-09-01', 'to': '2026-09-30'}).json()['days']
    assert off == [{'date': '2026-09-21', 'off': [{'employeeId': world.demo['sita'], 'empCode': row['emp_code'],
                                                   'nameEn': row['name_en'], 'type': 'HOME', 'status': 'pending',
                                                   'halfDay': None, 'fromTime': '10:00', 'toTime': '12:00'}]}]


def test_half_day_preview_and_request(world):
    _home(world)
    q = {'employeeId': world.demo['sita'], 'leaveTypeId': world.t('HOME'), 'from': '2026-09-21', 'to': '2026-09-21',
         'halfDay': 'second_half'}
    p = world.sita.get(f'{L}/preview', q).json()
    assert p['days'] == 0.5 and p['balanceAfter'] == 9.5 and 'hours' not in p
    made = world.sita.post(f'{L}/requests', q)
    assert made.status_code == 201, made.json()
    row = world.sita.get(f'{L}/requests', {'scope': 'own'}).json()['requests'][0]
    assert row['half_day'] == 'second_half' and row['days_requested'] == '0.50' and row['from_time'] is None
    cal = world.hr.get(f'{L}/team-calendar', {'from': '2026-09-21', 'to': '2026-09-21'}).json()['days']
    assert cal[0]['off'][0]['halfDay'] == 'second_half' and 'fromTime' not in cal[0]['off'][0]
    assert world.sita.get(f'{L}/preview', {**q, 'halfDay': 'morning'}).json()['error'] == 'validation'


def test_event_leave_preview_has_no_bridge_key(world):
    p = world.sita.get(f'{L}/preview', {'leaveTypeId': world.t('MOURNING'), 'from': '2026-09-21', 'to': '2026-09-22'}).json()
    # the TS route answered {...calc, balance, available, balanceAfter} for event leave, without a bridge
    assert 'bridge' not in p and (p['days'], p['balance'], p['available'], p['balanceAfter']) == (2, 13, 13, 11)


# ---------------------------------------------------------------- balances (LeavePage strip, EmployeeDetailPage, adjust page)

def test_balance_explanation_prints_zero_like_javascript(world):
    _home(world)
    lines = {b['code']: b for b in world.hr.get(f'{L}/balances', {'employeeId': world.demo['sita']}).json()['balances']}
    home = lines['HOME']
    # JavaScript prints (-0).toFixed(2) as "0.00": nothing taken reads "taken 0.00", never "taken -0.00"
    assert home['explanation'] == ('Opening 0.00 on 1 Shrawan 2083 + accrued 10.00 + comp-off 0.00 + adjustments 0.00 '
                                   '− taken 0.00 + cancelled 0.00 − lapsed 0.00 − encashed 0.00 = 10.00 days')
    assert '-0.00' not in ''.join(b['explanation'] for b in lines.values())
    assert lines['MOURNING']['explanation'] == 'Up to 13 paid day(s) per event (statutory); 0.00 day(s) taken this fiscal year'
    for b in lines.values():
        for k in ('balance', 'available', 'pending', 'taken'):
            assert isinstance(b[k], (int, float)) and not isinstance(b[k], bool), (b['code'], k)
    assert world.hr.get(f'{L}/balances', {'employeeId': world.demo['sita'], 'asOf': '2026-13-01'}).json()['error'] == 'validation'


# ---------------------------------------------------------------- LeaveAdjustPage

def test_adjust_body_as_the_page_builds_it(world):
    body = {'employeeId': world.demo['sita'], 'leaveTypeId': world.t('CASUAL'), 'deltaDays': 1.25, 'reason': 'goodwill',
            'kind': 'adjustment', 'effectiveDate': '2026-09-01'}
    r = world.hr.post(f'{L}/adjust', body)
    assert r.status_code == 201, r.json()
    assert isinstance(r.json()['balance'], float) and r.json()['replacedAutomatic'] is None
    line = next(x for x in world.hr.get(f'{L}/ledger', {'employeeId': world.demo['sita']}).json()['ledger']
                if x['id'] == r.json()['id'])
    assert line['effective_date'] == '2026-09-01' and line['delta_days'] == '1.25' and line['note'] == 'goodwill'
    assert line['posted_at'].endswith('Z') and line['expires_on'] is None and line['reason'] == 'adjustment'
    assert world.hr.post(f'{L}/adjust', {**body, 'reason': 'ab'}).json()['error'] == 'validation'
    assert world.hr.post(f'{L}/adjust', {**body, 'kind': 'gift'}).json()['error'] == 'validation'


# ---------------------------------------------------------------- LeaveTypesPage drawer

def test_statutory_type_saved_from_the_drawer(world):
    lt = next(t for t in world.hr.get(f'{L}/types').json()['types'] if t['code'] == 'HOME')
    # LeaveTypesPage.open() + save(): locked entitlement keys removed, empty strings and undefined dropped, null kept
    body = {'code': lt['code'], 'nameEn': lt['name_en'], 'nameNe': lt['name_ne'], 'carryForward': lt['carry_forward'],
            'encashable': lt['encashable'], 'allowHalfDay': lt['allow_half_day'], 'allowHourly': True,
            'minServiceDays': lt['min_service_days'], 'negativeBalanceLimit': float(lt['negative_balance_limit']),
            'sandwichRule': True, 'clubWithId': None}
    r = world.hr.patch(f"{L}/types/{lt['id']}", body)
    assert r.status_code == 200, r.json()
    after = next(t for t in world.hr.get(f'{L}/types').json()['types'] if t['id'] == lt['id'])
    assert after['sandwich_rule'] is True and after['allow_hourly'] is True and after['accrual_days'] == lt['accrual_days']


def test_company_type_create_club_and_unclub(world):
    body = {'code': 'STUDY', 'nameEn': 'Study leave', 'nameNe': 'अध्ययन', 'paid': True, 'accrualBasis': 'annual',
            'accrualDays': 5, 'carryForward': False, 'encashable': False, 'allowHalfDay': True, 'minServiceDays': 0,
            'negativeBalanceLimit': 0, 'genderScope': 'all', 'sandwichRule': False, 'clubWithId': None}
    made = world.hr.post(f'{L}/types', body)
    assert made.status_code == 201, made.json()
    sid = made.json()['id']
    clubbed = world.hr.patch(f'{L}/types/{sid}', {**body, 'clubWithId': world.t('CASUAL'), 'maxBalance': 12.5})
    assert clubbed.status_code == 200, clubbed.json()
    row = next(t for t in world.hr.get(f'{L}/types').json()['types'] if t['id'] == sid)
    assert row['club_with_id'] == world.t('CASUAL') and row['max_balance'] == '12.50' and row['accrual_days'] == '5.00'
    # clearing the "Club with" select sends clubWithId: null, which unclubs
    assert world.hr.patch(f'{L}/types/{sid}', {**body, 'clubWithId': None}).status_code == 200
    row = next(t for t in world.hr.get(f'{L}/types').json()['types'] if t['id'] == sid)
    assert row['club_with_id'] is None
    selfclub = world.hr.patch(f'{L}/types/{sid}', {'clubWithId': sid})
    assert selfclub.status_code == 400 and selfclub.json()['error'] == 'a leave type cannot be clubbed with itself'
    dup = world.hr.post(f'{L}/types', body)
    assert dup.status_code == 409 and dup.json()['error'] == 'already exists'


# ---------------------------------------------------------------- RequestsPage: HR lists and cancels approved leave

def test_hr_lists_all_and_cancels_with_a_reason(world):
    _home(world)
    made = world.sita.post(f'{L}/requests', {'leaveTypeId': world.t('HOME'), 'from': '2026-09-21', 'to': '2026-09-22'})
    rid = made.json()['id']
    rows = world.hr.get(f'{L}/requests', {'scope': 'all', 'status': 'pending'}).json()['requests']
    assert [r['id'] for r in rows] == [rid] and rows[0]['type_name'] == 'Home leave' and ISO.match(rows[0]['from_date'])
    assert world.hr.get(f'{L}/requests', {'scope': 'all', 'status': 'approved'}).json()['requests'] == []
    assert world.ram.get(f'{L}/requests', {'scope': 'team'}).json()['requests'][0]['id'] == rid
    r = world.hr.post(f'{L}/requests/{rid}/cancel', {'reason': 'cancelled by HR'})
    assert r.status_code == 200 and r.json() == {'ok': True}
    row = world.hr.get(f'{L}/requests', {'scope': 'all'}).json()['requests'][0]
    assert row['status'] == 'cancelled' and row['decided_at'].endswith('Z')
    long = world.hr.post(f'{L}/requests/{rid}/cancel', {'reason': 'x' * 501})
    assert long.status_code == 400 and long.json()['error'] == 'validation'
