"""Inbox card text (ApprovalsPage.tsx Summary), the SLA reminder hook and the flows page with flows on it."""
from datetime import datetime, timedelta, timezone

import pytest
from django.test import Client

from tests.support.sql import execute, one
from hajiri.workflow import flow_admin
from hajiri.workflow.cards import card, register_card

from .helpers import file_leave, unit


def _item(t, s, routing=()):
    return {'objectType': t, 'objectId': 'x', 'summary': s, 'routing': list(routing)}


def test_cards_for_every_built_in_type():
    c = card(_item('leave_request', {'leaveTypeName': 'Home leave', 'from': '2026-11-02', 'to': '2026-11-03', 'days': 2,
                                     'balanceNow': 5, 'balanceAfter': 3, 'teamOverlap': ['Ram'], 'reason': 'trip'}, ['Ram is on leave']))
    assert 'Home leave' in c['main'] and '2 days' in c['main']
    assert ('Balance 5.0 to 3.0', None) in c['extra'] and ('Also off: Ram', 'warn') in c['extra']
    assert ('Ram is on leave', 'warn') in c['extra'] and c['label'] == 'Leave' and c['tone'] == 'info'
    c = card(_item('leave_request', {'leaveTypeName': 'Sick', 'from': '2026-11-02', 'to': '2026-11-02', 'days': 0.25,
                                     'fromTime': '10:00', 'toTime': '12:00', 'hours': 2}))
    assert '10:00–12:00' in c['main'] and '2 h, 0.25 of a day' in c['main']
    c = card(_item('regularisation_request', {'kind': 'wfh', 'local_date': '2026-11-02'}))
    assert c['label'] == 'Work from home' and c['main'].startswith('Working from home on ')
    assert 'clock-in at' in card(_item('regularisation_request', {'kind': 'missed_punch', 'local_date': '2026-11-02',
                                                                    'punch_time': '2026-11-02T04:15:00Z', 'punch_direction': 'IN'}))['main']
    assert card(_item('ot_request', {'requested_min': 90, 'local_date': '2026-11-02'}))['main'].startswith('1 h 30 m overtime on')
    assert 'Monthly run for' in card(_item('payroll_run', {'run_type': 'monthly', 'bs_year': 2083, 'bs_month': 7, 'payslips': 3, 'total_net': '1000'}))['main']
    assert card(_item('letter_request', {'letter': 'Salary certificate', 'purpose': 'visa'}))['extra'] == [('For visa', None)]
    assert 'Hire 2 × Driver' in card(_item('job_requisition', {'headcount': 2, 'title': 'Driver'}))['main']
    assert 'Offer to Hari as Clerk' in card(_item('job_offer', {'candidate': 'Hari', 'designation': 'Clerk', 'salary': 30000, 'join_date': '2026-12-01'}))['main']
    p = card(_item('profile_change', {'sectionLabel': 'Bank', 'byHr': True, 'changes': [{'label': 'Account', 'from': None, 'to': '123'}]}))
    assert p['main'] == 'Bank (HR edit, second approval)' and ('Account: (empty) → 123', None) in p['extra']
    assert card(_item('training_nomination', {'course_title': 'Excel', 'cost': '0'}))['main'] == 'Join Excel'
    t = card(_item('tax_declaration', {'fiscalYearStart': 2083, 'lifePremium': 25000, 'proofs': 2}))
    assert t['main'] == 'Tax savings for FY 2083/84: Life insurance NPR 25,000.00'
    assert 'Review lines' in str(card(_item('expense_claim', {'title': 'Taxi', 'amount': 500, 'items': 1, 'receipts': 1}))['extra'][-1][0])
    assert 'Trip to Pokhara' in card(_item('travel_request', {'destination': 'Pokhara', 'from': '2026-11-02', 'to': '2026-11-04', 'days': 3}))['main']
    assert 'recover' in card(_item('travel_settlement', {'destination': 'Pokhara', 'net': -500, 'bills': 1}))['main']
    assert 'Swap shifts with Ram' in card(_item('shift_swap', {'colleague_name': 'Ram', 'local_date': '2026-11-02'}))['main']
    assert 'logged' in card(_item('timesheet', {'totalMin': 600, 'weekStart': '2026-11-02', 'weekEnd': '2026-11-08'}))['main']
    register_card('custom_thing', lambda it: {'main': 'Custom', 'extra': ['x']})
    assert card(_item('custom_thing', {}))['main'] == 'Custom'


@pytest.mark.usefixtures('leave_handler')
def test_sla_reminder_goes_to_the_line_manager(demo, login_as):
    from hajiri.worker.tasks import sla_reminders
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    ram = login_as(['manager'], demo['ram'])
    f = file_leave(demo['company_id'], demo['sita'], '2026-11-30')
    execute("UPDATE workflow_instance SET created_at = now() - interval '30 hours' WHERE id = %s", [f['instanceId']])
    out = sla_reminders(datetime.now(timezone.utc) + timedelta(seconds=1))
    assert out['reminded'] == 1
    assert one("SELECT user_id FROM notification_outbox WHERE template = 'workflow.sla' AND channel = 'in_app'")['user_id'].hex \
        == ram.user_id.replace('-', '')


def test_flows_page_shows_flows(demo, login_as):
    hr = login_as(['hr'])
    u = unit(demo['company_id'], 'OPS')
    flow_admin.create_flow(hr.user, {'objectType': 'leave_request', 'name': 'Ops staff', 'conditions': {'orgUnitIds': [u]},
                                     'steps': [{'kind': 'department_head'}, {'kind': 'role', 'role': 'hr'}], 'priority': 2, 'active': False})
    c = Client()
    c.cookies['hajiri_session'] = hr.token
    html = c.get('/settings/approvals?open=leave_request').content.decode()
    assert 'Ops staff' in html and 'For in OPS' in html and 'Department head → HR' in html and 'Priority 2' in html
    assert '1 more flow' in html
