"""The Overtime review page (OtSlipsPage.tsx), its HTMX actions, the request fragments and the legacy address."""
from __future__ import annotations

from hajiri.db.models import OtRequest, OtSlip
from hajiri.web import format as fmt
from tests.web.factories import htmx, hx_triggers, toasts

from .helpers import browser, days_ago, slip


def test_overtime_page_lists_slips_needing_review(world):
    c = browser(world.hr.token)
    r = c.get('/attendance/overtime')
    assert r.status_code == 200
    body = r.content.decode()
    assert 'Only approved hours reach payroll.' in body
    assert 'No overtime waiting for review' in body
    assert '0 slips' in body
    slip(world.demo['ram'], days_ago(2), 135, 'weekly_off')
    body = c.get('/attendance/overtime').content.decode()
    assert 'Ram Thapa' in body and 'E002' in body and '2 h 15 m' in body and 'Weekly off' in body
    assert fmt.fmt_date_long(days_ago(2)) in body
    assert '1 slip' in body and 'Give comp-off' in body and 'Export CSV' in body


def test_filters_change_the_empty_state(world):
    c = browser(world.hr.token)
    body = c.get('/attendance/overtime', {'status': 'approved'}).content.decode()
    assert 'No overtime in this range' in body and 'Try a wider date range or another status.' in body
    slip(world.demo['ram'], '2026-01-05', 60)
    assert 'Ram Thapa' not in c.get('/attendance/overtime').content.decode()       # older than 30 days
    body = c.get('/attendance/overtime', {'from': '2026-01-01', 'to': '2026-01-31', 'status': ''}).content.decode()
    assert 'Ram Thapa' in body


def test_page_is_for_the_attendance_area(world):
    assert browser(world.sita.token).get('/attendance/overtime').status_code != 200
    assert browser(world.ram.token).get('/attendance/overtime').status_code != 200
    admin = world.login_as(['attendance_admin'])
    assert browser(admin.token).get('/attendance/overtime').status_code == 200


def test_legacy_address_redirects(world):
    r = browser(world.hr.token).get('/hr/ot-slips')
    assert r.status_code in (301, 302) and r['Location'].startswith('/attendance/overtime')


def test_approve_modal(world):
    c = browser(world.hr.token)
    sid = slip(world.demo['ram'], '2026-09-21', 120)
    r = c.get(f'/_/ot/slips/{sid}/approve', **htmx())
    body = r.content.decode()
    assert r.status_code == 200 and 'Approve overtime' in body and 'Detected 2 h.' in body
    assert 'Cannot exceed the detected minutes or the 4 h daily cap.' in body and 'max="120"' in body
    assert c.post(f'/_/ot/slips/{sid}/approve', {'approved_min': '300'}, **htmx()).status_code == 422
    r = c.post(f'/_/ot/slips/{sid}/approve', {'approved_min': '90'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Overtime approved.'
    assert 'hj:refresh-ot-slips' in hx_triggers(r)
    assert OtSlip.objects.values('approved_min', 'status').get(pk=sid) == {'approved_min': 90, 'status': 'approved'}
    # Approving again is a conflict shown as a toast.
    r = c.post(f'/_/ot/slips/{sid}/approve', {'approved_min': '60'}, **htmx())
    assert r.status_code == 409 and toasts(r)[0]['message'] == 'slip is already approved'


def test_reject_modal(world):
    c = browser(world.hr.token)
    sid = slip(world.demo['ram'], '2026-09-21', 120)
    body = c.get(f'/_/ot/slips/{sid}/reject', **htmx()).content.decode()
    assert 'Reject overtime' in body and 'The employee will see your reason.' in body and 'At least 3 characters.' in body
    assert c.post(f'/_/ot/slips/{sid}/reject', {'reason': 'no'}, **htmx()).status_code == 422
    r = c.post(f'/_/ot/slips/{sid}/reject', {'reason': 'not authorised'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Overtime rejected.'
    assert OtSlip.objects.get(pk=sid).status == 'rejected'


def test_comp_off_button(world):
    c = browser(world.hr.token)
    sid = slip(world.demo['ram'], '2026-09-26', 240, 'weekly_off')
    r = c.post(f'/_/ot/slips/{sid}/comp-off', **htmx())
    assert r.status_code == 204
    assert toasts(r)[0]['message'] == f"Credited 0.5 days of comp-off, expires {fmt.fmt_date('2026-10-17')}."
    sid = slip(world.demo['ram'], '2026-09-27', 480, 'weekly_off')
    r = c.post(f'/_/ot/slips/{sid}/comp-off', **htmx())
    assert toasts(r)[0]['message'].startswith('Credited 1 day of comp-off')


def test_manager_decides_their_report_from_the_fragments(world, other_mgr):
    sid = slip(world.demo['sita'], '2026-10-01', 60)
    r = browser(other_mgr.token).post(f'/_/ot/slips/{sid}/approve', {'approved_min': '30'}, **htmx())
    assert r.status_code == 403
    r = browser(world.sita.token).post(f'/_/ot/slips/{sid}/approve', {'approved_min': '30'}, **htmx())
    assert r.status_code == 403
    r = browser(world.ram.token).post(f'/_/ot/slips/{sid}/approve', {'approved_min': '30'}, **htmx())
    assert r.status_code == 204


def test_register_csv_download(world):
    slip(world.demo['ram'], '2026-09-26', 60)
    r = browser(world.hr.token).get('/_/ot/register', {'from': '2026-09-01', 'to': '2026-09-30'})
    assert r.status_code == 200 and r['Content-Disposition'] == 'attachment; filename="ot-register-2026-09-01_2026-09-30.csv"'
    text = r.content.decode()
    assert text.startswith('﻿emp_code,name_en,') and 'E002,Ram Thapa,2026-09-26' in text
    assert browser(world.ram.token).get('/_/ot/register', {'from': '2026-09-01', 'to': '2026-09-30'}).status_code == 403


def test_request_fragments(world):
    c = browser(world.sita.token)
    body = c.get('/_/ot/requests/new', **htmx()).content.decode()
    assert 'Request overtime' in body and 'employee_id' not in body
    assert 'employee_id' in browser(world.ram.token).get('/_/ot/requests/new', **htmx()).content.decode()
    assert c.post('/_/ot/requests/new', {'local_date': days_ago(1), 'requested_min': '0'}, **htmx()).status_code == 422
    r = c.post('/_/ot/requests/new', {'local_date': days_ago(1), 'requested_min': '300'}, **htmx())
    assert r.status_code == 400 and 'overtime may not exceed 240 minutes' in toasts(r)[0]['message']
    r = c.post('/_/ot/requests/new', {'local_date': days_ago(1), 'requested_min': '90', 'reason': 'audit'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Overtime request sent for approval.'
    assert 'hj:refresh-ot-requests' in hx_triggers(r)
    rid = str(OtRequest.objects.get(employee_id=world.demo['sita']).id)
    body = c.get('/_/ot/requests', **htmx()).content.decode()
    assert '1 h 30 m' in body and 'Cancel' in body
    assert 'No cancelled overtime requests' in c.get('/_/ot/requests', {'status': 'cancelled'}, **htmx()).content.decode()
    r = c.post(f'/_/ot/requests/{rid}/cancel', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Overtime request cancelled.'
    assert OtRequest.objects.get(pk=rid).status == 'cancelled'


def test_nav_tab_and_palette(world):
    from hajiri.ot.nav import NAV
    assert [(n.hub, n.label, n.url_name, n.order, n.module) for n in NAV] == [('attendance', 'Overtime', 'ot:overtime', 40, 'attendance')]
    body = browser(world.hr.token).get('/attendance/overtime').content.decode()
    assert 'Overtime review' in body


def test_slip_count_reloads_with_the_table_after_a_decision(world):
    """TS reloaded the whole query after an action, so the 'N slips' count follows the table straight away."""
    c = browser(world.hr.token)
    slip(world.demo['ram'], days_ago(2), 60)
    body = c.get('/attendance/overtime').content.decode()
    assert 'hx-trigger="hj:refresh-ot-slips from:body" hx-select="#ot-slip-count"' in body
    sid = slip(world.demo['ram'], days_ago(3), 60)
    c.post(f'/_/ot/slips/{sid}/approve', {'approved_min': '30'}, **htmx())
    r = c.get('/attendance/overtime', {'status': 'detected'}, **htmx(target='ot-slip-count'))
    assert '1 slip' in r.content.decode()


def test_emptied_minutes_field_approves_zero_like_the_ts_modal(world):
    c = browser(world.hr.token)
    sid = slip(world.demo['ram'], '2026-09-21', 120)
    r = c.post(f'/_/ot/slips/{sid}/approve', {'approved_min': ''}, **htmx())
    assert r.status_code == 204
    assert OtSlip.objects.values('approved_min', 'status').get(pk=sid) == {'approved_min': 0, 'status': 'approved'}
