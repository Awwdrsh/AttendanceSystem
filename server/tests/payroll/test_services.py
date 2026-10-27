"""Payroll rules the TS tests do not cover one by one: held pay never by its owner and only before lock, certificates
showing published pay only, tax savings through the approval workflow, proofs, the HR-only approval and the seeder."""
from __future__ import annotations

import base64

import pytest

from hajiri.core.errors import ConflictError, ForbiddenError
from hajiri.payroll import api, compliance, tax_savings
from hajiri.payroll.seeds import seed_payroll_defaults

from .helpers import attendance_for, execute, get, master_data, one, post, rows

PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').decode()


@pytest.fixture
def s(demo, login_as):
    hr_ram = login_as(['hr'], demo['ram'])       # an HR user who is also the employee Ram
    hr = login_as(['hr'])
    sita = login_as(['employee'], demo['sita'])
    e3 = one("""INSERT INTO employee (company_id, emp_code, name_en, join_date) VALUES (%s, 'E003', 'Hari Karki', '2026-10-01') RETURNING id""", [demo['company_id']])['id']
    colleague = login_as(['employee'], str(e3))
    master_data(demo)
    std = get(hr.token, '/structures').body['structures'][0]['versions'][0]['id']
    for emp, base in ((demo['sita'], {'BASIC': '40000.00', 'ALLOWANCE': '20000.00'}), (demo['ram'], {'BASIC': '30000.00', 'DA': '7380.00'})):
        post(hr.token, '/assignments', {'employeeId': emp, 'structureVersionId': std, 'from': '2026-01-01', 'baseAmounts': base})
    attendance_for(demo['sita'], '2026-08-17', '2026-09-16')
    attendance_for(demo['ram'], '2026-08-17', '2026-09-16')
    p = post(hr.token, '/periods', {'bsYear': 2083, 'bsMonth': 5}).body['period']['id']
    run_id = post(hr.token, '/runs', {'periodId': p}).body['id']
    return {'demo': demo, 'hr': hr, 'hr_ram': hr_ram, 'sita': sita, 'colleague': colleague, 'run': run_id, 'login_as': login_as}


def test_pay_is_held_only_after_calculation_and_before_lock_and_never_by_its_owner(s):
    hr, demo, run = s['hr'].token, s['demo'], s['run']
    r = post(hr, f'/runs/{run}/holds', {'employeeId': demo['sita'], 'reason': 'documents pending'})
    assert r.status == 409 and r.body['error'] == "calculate the run before holding anyone's pay"
    assert post(hr, f'/runs/{run}/compute').status == 200
    own = post(s['hr_ram'].token, f'/runs/{run}/holds', {'employeeId': demo['ram'], 'reason': 'documents pending'})
    assert own.status == 403 and own.body['error'] == 'someone else must hold or release your own pay'
    assert post(hr, f'/runs/{run}/holds', {'employeeId': demo['ram'], 'reason': 'ok'}).status == 400  # reason too short
    h = post(hr, f'/runs/{run}/holds', {'employeeId': demo['ram'], 'reason': 'documents pending'})
    assert h.status == 201
    assert 'hold' in [x['action'] for x in rows('SELECT action FROM audit_log WHERE row_id = %s', [h.body['id']])]
    post(hr, f'/runs/{run}/review')
    post(hr, f'/runs/{run}/approve')
    post(hr, f'/runs/{run}/lock')
    late = post(hr, f'/runs/{run}/holds', {'employeeId': demo['sita'], 'reason': 'documents pending'})
    assert late.status == 409 and late.body['error'] == 'this run is locked and paid; pay can only be held before the run is locked'
    # The owner cannot release their own held pay either.
    rel = post(s['hr_ram'].token, f"/holds/{h.body['id']}/release", {'mode': 'outside'})
    assert rel.status == 403 and rel.body['error'] == 'someone else must hold or release your own pay'
    ok = post(hr, f"/holds/{h.body['id']}/release", {'mode': 'outside', 'note': 'cheque'})
    assert ok.status == 200
    assert one("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'payroll.hold_released' AND employee_id = %s",
               [demo['ram']])['n'] > 0
    assert one('SELECT status, release_mode FROM payroll_hold WHERE id = %s', [h.body['id']]) == {'status': 'released', 'release_mode': 'outside'}


def test_certificates_show_published_pay_only_to_the_employee(s):
    hr, demo, run = s['hr'].token, s['demo'], s['run']
    for step in ('compute', 'review', 'approve', 'lock'):
        assert post(hr, f'/runs/{run}/{step}').status == 200
    mine = get(s['sita'].token, '/certificates/me', {'kind': 'tax', 'fiscalYearStart': 2083})
    assert mine.status == 200
    assert 'No locked payslips in this fiscal year.' in mine.text and 'Bhadra 2083' not in mine.text
    by_hr = get(hr, f"/certificates/{demo['sita']}", {'kind': 'tax', 'fiscalYearStart': 2083})
    assert 'Bhadra 2083' in by_hr.text
    sal = get(s['sita'].token, '/certificates/me', {'kind': 'salary'})
    assert 'Current monthly salary' not in sal.text
    post(hr, f'/runs/{run}/publish')
    assert 'Bhadra 2083' in get(s['sita'].token, '/certificates/me', {'kind': 'tax', 'fiscalYearStart': 2083}).text
    assert 'Current monthly salary (as of Bhadra 2083)' in get(s['sita'].token, '/certificates/me', {'kind': 'salary'}).text


def test_run_guards_and_outputs(s):
    hr, run = s['hr'].token, s['run']
    csv = get(hr, f'/runs/{run}/salary-sheet', {'format': 'csv'})
    assert csv.status == 409 and csv.body['error'] == 'this run has no payslips yet; calculate it first'
    assert get(hr, f'/runs/{run}/salary-sheet').status == 200  # JSON is fine before calculation
    assert post(hr, f'/runs/{run}/publish').body['error'] == 'only locked runs can be published'
    assert post(hr, f'/runs/{run}/lock').body['error'] == 'run is draft; approve it first'
    assert post(hr, f'/runs/{run}/review').body['error'] == 'run is draft; compute it first'
    post(hr, f'/runs/{run}/compute')
    post(hr, f'/runs/{run}/review')
    # A payroll admin may prepare a run but only HR approves it.
    pa = s['login_as'](['payroll_admin']).token
    r = post(pa, f'/runs/{run}/approve')
    assert r.status == 403 and r.body['error'] == 'approval requires the HR role'
    assert one("SELECT count(*)::int AS n FROM audit_log WHERE action = 'salary_sheet' AND row_id = %s", [run])['n'] >= 1
    # Arrears runs need the run they correct, which must be locked.
    p = one('SELECT period_id FROM payroll_run WHERE id = %s', [run])['period_id']
    assert post(hr, '/runs', {'periodId': str(p), 'runType': 'arrears'}).body['error'] == 'arrears runs need referencesRunId'
    assert post(hr, '/runs', {'periodId': str(p), 'runType': 'arrears', 'referencesRunId': run}).body['error'] == 'referenced run must be locked'


def test_tax_savings_requests_go_through_the_approval_workflow(s):
    from hajiri.files.service import upload
    demo, sita = s['demo'], s['sita']
    fy = tax_savings.current_fiscal_year_start()
    assert post(sita.token, '/tax-savings', {'fiscalYearStart': fy + 2}).body['error'] == \
        'declarations can be made for the current, previous or next fiscal year only'
    proof = upload(sita.user, {'purpose': 'tax_proof', 'filename': 'premium.pdf', 'dataUrl': PDF})['id']
    first = post(sita.token, '/tax-savings', {'fiscalYearStart': fy, 'lifePremium': '30000.00', 'note': 'policy 1', 'proofFileIds': [proof]})
    assert first.status == 201
    # A new submission replaces the one HR has not decided yet.
    second = post(sita.token, '/tax-savings', {'fiscalYearStart': fy, 'lifePremium': '35000.00'})
    assert second.status == 201
    assert one('SELECT status, decision_note FROM tax_declaration_request WHERE id = %s', [first.body['request']['id']]) == \
        {'status': 'cancelled', 'decision_note': 'replaced by a newer submission'}
    lst = get(sita.token, '/tax-savings', {'fiscalYearStart': fy})
    assert lst.body['pending']['id'] == second.body['request']['id']
    # (both rows share the test transaction's now(), so look the first one up by id rather than by position)
    old = next(h for h in lst.body['history'] if h['id'] == first.body['request']['id'])
    assert old['proofs'][0]['filename'] == 'premium.pdf'
    # The proof opens for the employee and HR (HR views are audited), not for a colleague.
    rid, url = first.body['request']['id'], f"/tax-savings/{first.body['request']['id']}/files/{proof}"
    assert get(sita.token, url).status == 200
    assert get(s['colleague'].token, url).status == 404
    assert get(s['hr'].token, url).status == 200
    assert 'view_file' in [x['action'] for x in rows('SELECT action FROM audit_log WHERE row_id = %s', [rid])]
    # Someone else's estimate is HR's only.
    assert post(s['colleague'].token, '/tax-savings/estimate', {'employeeId': demo['sita']}).status == 403
    # HR approves from the approvals inbox: the workflow handler writes the verified declaration and tells Sita.
    from hajiri.workflow.service import decide, get_instance, summarise
    inst = one('SELECT workflow_instance_id FROM tax_declaration_request WHERE id = %s', [second.body['request']['id']])['workflow_instance_id']
    card = summarise(get_instance(str(inst)))
    assert card['lifePremium'] == '35000.00' and card['fiscalYearStart'] == fy and card['current'] is None
    decide(s['hr'].user, str(inst), 'approve')
    d = one('SELECT life_premium::text, status FROM tax_declaration WHERE employee_id = %s AND fiscal_year_start = %s', [demo['sita'], fy])
    assert d == {'life_premium': '35000.00', 'status': 'verified'}
    assert one("SELECT title FROM notification_outbox WHERE template = 'tax_declaration.approved' AND employee_id = %s LIMIT 1",
               [demo['sita']])['title'] == 'Tax savings approved'
    # Withdrawing a decided request is refused.
    r = post(sita.token, f"/tax-savings/{second.body['request']['id']}/cancel")
    assert r.status == 409 and r.body['error'] == 'request already approved'


def test_tax_savings_rejection_tells_the_employee_why(s):
    demo, sita = s['demo'], s['sita']
    fy = tax_savings.current_fiscal_year_start()
    post(sita.token, '/tax-savings', {'fiscalYearStart': fy, 'healthPremium': '20000.00'})
    r = post(s['hr'].token, f"/declarations/{demo['sita']}/verify", {'fiscalYearStart': fy, 'decision': 'rejected', 'reason': 'receipt missing'})
    assert r.status == 200
    n = one("SELECT body FROM notification_outbox WHERE template = 'tax_declaration.rejected' AND employee_id = %s LIMIT 1", [demo['sita']])
    assert n['body'].endswith(': receipt missing.')
    assert one('SELECT 1 AS x FROM tax_declaration WHERE employee_id = %s AND fiscal_year_start = %s', [demo['sita'], fy]) is None


def test_own_declaration_is_a_request_and_drafts_are_not_kept(s):
    fy = tax_savings.current_fiscal_year_start()
    r = api.put_declaration(s['hr_ram'].user, {'fiscalYearStart': fy, 'lifePremium': '1000.00'})
    assert r['status'] == 'pending'
    with pytest.raises(Exception) as e:
        api.put_declaration(s['sita'].user, {'fiscalYearStart': fy, 'submit': False})
    assert str(e.value) == 'drafts are not kept; submit the declaration when it is ready'


def test_payroll_seeder_is_idempotent(demo):
    again = seed_payroll_defaults(demo['company_id'])
    assert again == {'rules': 0, 'components': 0, 'structures': 0}
    assert one("SELECT count(*)::int AS n FROM salary_component WHERE company_id = %s AND statutory", [demo['company_id']])['n'] == 6


def test_compliance_functions_refuse_people_outside_payroll(s):
    for fn, args in ((compliance.list_holds, ({},)), (compliance.optimum_all, ()), (compliance.annual_tax, ({},)),
                     (compliance.list_openings, ({},))):
        with pytest.raises(ForbiddenError) as e:
            fn(s['sita'].user, *args)
        assert e.value.message == 'requires role hr or payroll_admin'
    with pytest.raises(ConflictError):
        compliance.cancel_hold(s['hr'].user, _held_and_locked(s))
    execute('SELECT 1')


def _held_and_locked(s) -> str:
    hr, demo, run = s['hr'].token, s['demo'], s['run']
    post(hr, f'/runs/{run}/compute')
    h = post(hr, f'/runs/{run}/holds', {'employeeId': demo['ram'], 'reason': 'check bank'}).body['id']
    for step in ('review', 'approve', 'lock'):
        post(hr, f'/runs/{run}/{step}')
    return h


def test_rule_source_must_be_a_web_link(demo, login_as):
    """routes.ts POST /rules: sourceUrl is z.string().url() and must be http(s); a supersedesId closes the old version."""
    admin = login_as(['admin']).token
    base = {'ruleKey': 'test.src', 'validFromAd': '2026-01-01', 'validFromBs': '2082-09-17', 'value': {'x': 1}}
    bad = post(admin, '/rules', {**base, 'sourceUrl': 'http://'})
    assert bad.status == 400 and bad.body['issues'][0]['message'] == 'Invalid URL'
    ftp = post(admin, '/rules', {**base, 'sourceUrl': 'ftp://example.com/x'})
    assert ftp.status == 400 and ftp.body['issues'][0]['message'] == 'source must be an http(s) link'
    first = post(admin, '/rules', {**base, 'sourceUrl': 'https://ird.gov.np/circular'})
    assert first.status == 201
    second = post(admin, '/rules', {**base, 'validFromAd': '2026-07-17', 'validFromBs': '2083-04-01', 'supersedesId': first.body['id']})
    assert second.status == 201
    assert str(one('SELECT valid_to_ad FROM rule_version WHERE id = %s', [first.body['id']])['valid_to_ad']) == '2026-07-16'
