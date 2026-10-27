"""Port of the 'tax savings self-service' block of apps/api/test/security-selfservice.test.ts (the other blocks of that
file belong to the auth and leave slices). One test with the TS steps in order, through the JSON API as the TS test."""
from __future__ import annotations

import base64
import json

from django.test import Client

from engines.bs_calendar import ad_to_bs, fiscal_year_of
from hajiri.core.clock import npt_today

from .helpers import Res, execute, get, one, post, put, rows

# The TS test used FY 2083, the fiscal year current when it was written. Use the current one, so the test keeps passing
# after Shrawan 2084 (the self-service endpoints only accept the current fiscal year).
_FY = fiscal_year_of(ad_to_bs(npt_today().isoformat()))
FY = _FY.start_year
PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n%%EOF').decode()


def _wf_get(tok, path):
    return Res(Client().get(f'/api/v1/workflow{path}', HTTP_AUTHORIZATION=f'Bearer {tok}'))


def _wf_post(tok, path, body=None):
    return Res(Client().post(f'/api/v1/workflow{path}', json.dumps(body or {}), content_type='application/json',
                             HTTP_AUTHORIZATION=f'Bearer {tok}'))


def test_tax_savings_self_service(demo, login_as):
    from hajiri.files.service import upload
    hr = login_as(['hr']).token
    sita_l = login_as(['employee'], demo['sita'])
    sita = sita_l.token
    ram_l = login_as(['manager'], demo['ram'])
    ram = ram_l.token
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    other = one("INSERT INTO company (code, name_en) VALUES ('OTHERJ', 'Other Co') RETURNING id")['id']
    other_admin = login_as(['admin'], company_id=str(other), email='admin@other-j.local').token

    # beforeAll: Sita earns 150,000 a month from the start of the fiscal year (FY 2083 in the TS test)
    ver = get(hr, '/structures').body['structures'][0]['versions'][0]['id']
    a = post(hr, '/assignments', {'employeeId': demo['sita'], 'structureVersionId': ver, 'from': _FY.start_ad, 'baseAmounts': {'BASIC': '150000.00'}})
    assert a.status == 201

    # estimates tax before and after the proposed savings using the payroll engine
    e = post(sita, '/tax-savings/estimate', {'fiscalYearStart': FY, 'proposed': {'lifePremium': '40000.00', 'healthPremium': '20000.00'}})
    assert e.status == 200
    assert e.body['available'] is True
    assert float(e.body['current']['annualTax']) > 0
    assert e.body['proposed']['insuranceDeduction'] == '60000.00'
    assert float(e.body['annualSaving']) > 0
    assert float(e.body['proposed']['annualTax']) < float(e.body['current']['annualTax'])
    # Without a salary there is nothing to estimate; another person's estimate is HR only.
    ram_est = post(ram, '/tax-savings/estimate', {})
    assert ram_est.body['available'] is False
    assert post(ram, '/tax-savings/estimate', {'employeeId': demo['sita']}).status == 403
    assert post(hr, '/tax-savings/estimate', {'employeeId': demo['sita']}).body['available'] is True

    # submitting with a proof creates a pending request that does not touch payroll
    proof_id = upload(sita_l.user, {'purpose': 'tax_proof', 'filename': 'premium-receipt.pdf', 'dataUrl': PDF})['id']
    neg = post(sita, '/tax-savings', {'fiscalYearStart': FY, 'lifePremium': '-1.00'})
    assert neg.status == 400
    r = post(sita, '/tax-savings', {'fiscalYearStart': FY, 'lifePremium': '40000.00', 'healthPremium': '20000.00', 'note': 'policy renewed',
                                    'proofFileIds': [proof_id]})
    assert r.status == 201
    request_id = r.body['request']['id']
    assert len(rows('SELECT * FROM tax_declaration WHERE employee_id = %s AND fiscal_year_start = %s', [demo['sita'], FY])) == 0
    mine = get(sita, '/tax-savings', {'fiscalYearStart': FY})
    assert mine.body['declaration'] is None
    assert mine.body['pending']['id'] == request_id and mine.body['pending']['life_premium'] == '40000.00'
    p0 = mine.body['history'][0]['proofs'][0]
    assert p0['id'] == proof_id and p0['filename'] == 'premium-receipt.pdf'
    # A proof already attached cannot be reused by someone else.
    steal = post(ram, '/tax-savings', {'fiscalYearStart': FY, 'proofFileIds': [proof_id]})
    assert steal.status == 400

    # proof files: the employee and HR (audited) only
    url = f'/tax-savings/{request_id}/files/{proof_id}'
    assert get(sita, url).status == 200
    assert get(ram, url).status == 404
    h = get(hr, url)
    assert h.status == 200
    assert 'application/pdf' in h.headers['Content-Type']
    assert len(rows("SELECT 1 FROM audit_log WHERE action = 'view_file' AND row_id = %s", [request_id])) == 1
    assert get(other_admin, url).status == 404

    # a new submission replaces the pending one; the manager cannot approve, HR can
    r2 = post(sita, '/tax-savings', {'fiscalYearStart': FY, 'lifePremium': '35000.00', 'healthPremium': '20000.00'})
    assert r2.status == 201
    assert one('SELECT status FROM tax_declaration_request WHERE id = %s', [request_id])['status'] == 'cancelled'
    request_id = r2.body['request']['id']
    ram_inbox = _wf_get(ram, '/inbox')
    assert ram_inbox.status == 200
    assert next((i for i in ram_inbox.body['items'] if i['objectId'] == request_id), None) is None
    inbox = _wf_get(hr, '/inbox')
    item = next(i for i in inbox.body['items'] if i['objectId'] == request_id)
    assert item['objectType'] == 'tax_declaration'
    assert item['summary']['fiscalYearStart'] == FY and item['summary']['lifePremium'] == '35000.00' and item['summary']['current'] is None
    act = _wf_post(hr, f"/{item['instanceId']}/approve", {})
    assert act.status == 200, act
    decl = one('SELECT status, life_premium::text FROM tax_declaration WHERE employee_id = %s AND fiscal_year_start = %s', [demo['sita'], FY])
    assert decl == {'status': 'verified', 'life_premium': '35000.00'}
    assert len(rows("SELECT 1 FROM notification_outbox WHERE template = 'tax_declaration.approved'")) > 0
    est = post(sita, '/tax-savings/estimate', {'fiscalYearStart': FY})
    assert est.body['current']['insuranceDeduction'] == '55000.00'

    # the old declarations endpoint: own submissions become requests; HR entry for others is verified directly
    own = put(sita, '/declarations', {'fiscalYearStart': FY, 'lifePremium': '10000.00'})
    assert own.body['ok'] is True and own.body['status'] == 'pending'
    assert one('SELECT life_premium::text FROM tax_declaration WHERE employee_id = %s AND fiscal_year_start = %s',
               [demo['sita'], FY])['life_premium'] == '35000.00'
    g = get(hr, '/declarations', {'employeeId': demo['sita'], 'fiscalYearStart': FY})
    assert g.body['pending']['life_premium'] == '10000.00'
    rej = post(hr, f"/declarations/{demo['sita']}/verify", {'fiscalYearStart': FY, 'decision': 'rejected', 'reason': 'no receipt'})
    assert rej.status == 200
    assert one('SELECT life_premium::text FROM tax_declaration WHERE employee_id = %s AND fiscal_year_start = %s',
               [demo['sita'], FY])['life_premium'] == '35000.00'
    hr_entry = put(hr, '/declarations', {'employeeId': demo['ram'], 'fiscalYearStart': FY, 'healthPremium': '15000.00'})
    assert hr_entry.body['ok'] is True and hr_entry.body['status'] == 'verified'

    # cannot cancel someone else's request; can cancel my own pending one
    r3 = post(sita, '/tax-savings', {'fiscalYearStart': FY, 'medicalCost': '5000.00'})
    rid = r3.body['request']['id']
    assert post(ram, f'/tax-savings/{rid}/cancel').status == 404
    assert post(sita, f'/tax-savings/{rid}/cancel').status == 200
    assert one('SELECT state FROM workflow_instance WHERE object_id = %s', [rid])['state'] == 'cancelled'
    assert get(ram, '/tax-savings', {'employeeId': demo['sita']}).status == 403
