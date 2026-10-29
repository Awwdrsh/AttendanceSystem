"""The attendance tools as the React app calls them (frontend/src/api/attendanceTools.js and the upload page's template
download): the session cookie from fetch() with no CSRF token. Writes pass from the site's own origin and are refused
from a foreign one with the TS message; the shapes the pages read are there; another company's records are not
found."""
from __future__ import annotations

from datetime import date, timedelta

from engines.bs_calendar import bs_month_bounds

from hajiri.core import auth
from hajiri.core.auth import create_user
from hajiri.leave.services import leave_type_by_code
from tests.core.fixtures import PW
from tests.punch.api import CROSS_SITE, Browser, post

GOOD = 'emp_code,date_bs,in_time,out_time,note\nE001,2083-06-01,9:58,18:05,device offline\n'
Y, M = 2083, 5


def _other_company_hr():
    from hajiri.db.models import Company
    c = Company.objects.create(code='ATR2', name_en='Other Co')
    create_user(company_id=str(c.id), employee_id=None, email='hr@atr2.local', name='Other HR', password=PW, roles=['hr'])
    return Browser(auth.login('hr@atr2.local', PW).token)


def test_rules_penalties_upload_issues_and_whos_out_with_the_cookie(demo, login_as):
    tok = login_as(['hr']).token
    hr, evil = Browser(tok), Browser(tok, origin='https://evil.example')
    sita = Browser(login_as(['employee'], demo['sita']).token)
    other = _other_company_hr()
    home = str(leave_type_by_code(demo['company_id'], 'HOME')['id'])

    # rules
    p = hr.get('/attendance-tools/policy')
    assert p.status == 200 and 'latePenalty' in p.body['policy'] and 'whosOutScope' in p.body['policy']
    rule = {'latePenalty': {'enabled': True, 'lateCount': 3, 'penaltyDays': 0.5, 'minLateMinutes': 5, 'leaveTypeIds': [home]}}
    assert evil.put('/attendance-tools/policy', rule).body == CROSS_SITE
    assert sita.put('/attendance-tools/policy', rule).status == 403
    ok = hr.put('/attendance-tools/policy', rule)
    assert ok.status == 200 and ok.body['policy']['latePenalty']['enabled'] is True
    assert other.put('/attendance-tools/policy', rule).status == 400          # HOME of another company does not exist

    # four late days for Ram, a quarter day of HOME balance
    start = date.fromisoformat(bs_month_bounds(Y, M)['startAd'])
    days = [d for d in (start + timedelta(days=i) for i in range(8)) if d.weekday() != 5][:4]
    for d in days:
        for time, direction in (('10:40', 'in'), ('18:00', 'out')):
            r = hr.post('/punch/manual', {'employeeId': demo['ram'], 'localDate': d.isoformat(), 'time': time,
                                          'direction': direction, 'reason': 'test data'})
            assert r.status == 201
    assert post(tok, '/leave/adjust', {'employeeId': demo['ram'], 'leaveTypeId': home, 'deltaDays': 0.25, 'reason': 'opening',
                                     'kind': 'allocation', 'effectiveDate': start.isoformat()}).status in (200, 201)

    # (the opening balance goes through the leave API with the Bearer token: setup, not under test here)

    # preview, apply, reverse (LatePenaltiesPage)
    pv = hr.get('/attendance-tools/penalties', {'bsYear': Y, 'bsMonth': M})
    assert pv.status == 200 and set(pv.body) == {'rule', 'locked', 'lines'} and pv.body['locked'] is False
    line = next(x for x in pv.body['lines'] if x['employeeId'] == demo['ram'])
    assert line['lateDays'] == 4 and line['applied'] is None
    assert evil.post('/attendance-tools/penalties/apply', {'bsYear': Y, 'bsMonth': M}).status == 403
    a = hr.post('/attendance-tools/penalties/apply', {'bsYear': Y, 'bsMonth': M, 'employeeIds': [demo['ram']]})
    assert a.status == 200 and a.body['applied'] == 1
    pid = next(x for x in hr.get('/attendance-tools/penalties', {'bsYear': Y, 'bsMonth': M}).body['lines']
               if x['employeeId'] == demo['ram'])['applied']['id']
    assert other.post(f'/attendance-tools/penalties/{pid}/reverse', {'reason': 'not yours at all'}).status == 404
    assert other.get('/attendance-tools/penalties', {'bsYear': Y, 'bsMonth': M}).body['lines'] == []
    assert hr.post(f'/attendance-tools/penalties/{pid}/reverse', {'reason': 'device clock was wrong'}).body == {'ok': True}

    # my late arrivals (home card)
    me = sita.get('/attendance-tools/penalties/me', {'bsYear': Y, 'bsMonth': M})
    assert me.status == 200 and set(me.body) >= {'bsYear', 'bsMonth', 'enabled', 'rule', 'lateDays', 'lateDates',
                                                 'projectedDays', 'applied'}

    # manual upload: template download, check, import, history
    tpl = hr.get('/attendance-tools/upload/template')
    assert tpl.status == 200 and 'text/csv' in tpl.headers['Content-Type']
    assert 'attendance-upload-template.csv' in tpl.headers['Content-Disposition']
    chk = hr.post('/attendance-tools/upload/check', {'csv': GOOD})
    assert chk.status == 200 and (chk.body['valid'], chk.body['invalid']) == (1, 0)
    assert evil.post('/attendance-tools/upload/import', {'csv': GOOD, 'reason': 'device outage'}).status == 403
    imp = hr.post('/attendance-tools/upload/import', {'csv': GOOD, 'reason': 'device outage', 'filename': 'aug.csv'})
    assert imp.status == 201 and imp.body['rows'] == 1 and imp.body['punches'] == 2
    up = hr.get('/attendance-tools/uploads')
    assert up.body['uploads'][0]['filename'] == 'aug.csv' and up.body['uploads'][0]['uploaded_by_name']
    assert other.get('/attendance-tools/uploads').body['uploads'] == []
    # E001 is not an employee code of the other company
    assert other.post('/attendance-tools/upload/check', {'csv': GOOD}).body['invalid'] == 1

    # issues, the pre-payroll summary and nudges
    iss = hr.get('/attendance-tools/issues', {'from': '2026-10-01', 'to': '2026-10-10'})
    assert iss.status == 200 and set(iss.body) == {'summary', 'rows'}
    s = hr.get('/attendance-tools/issues/summary', {'bsYear': 2083, 'bsMonth': 6})
    assert s.status == 200 and {'bsYear', 'bsMonth', 'from', 'to', 'absent'} <= set(s.body)
    assert evil.post('/attendance-tools/issues/nudge', {'target': 'approver', 'instanceId': pid}).body == CROSS_SITE
    nf = hr.post('/attendance-tools/issues/nudge', {'target': 'approver', 'instanceId': '00000000-0000-7000-8000-00000000dead'})
    assert nf.status == 404 and nf.body == {'error': 'request not found'}
    assert other.post('/attendance-tools/issues/nudge', {'target': 'employee', 'employeeId': demo['sita'], 'from': '2026-10-01',
                                                          'to': '2026-10-10'}).status == 404
    bad = hr.post('/attendance-tools/issues/nudge', {'target': 'nobody'})
    assert bad.status == 400 and bad.body['error'] == 'validation'

    # who's out (home card)
    w = sita.get('/attendance-tools/whos-out', {'days': 7})
    assert w.status == 200 and set(w.body) == {'scope', 'days'}
    assert sita.get('/attendance-tools/whos-out', {'days': 30}).status == 400
