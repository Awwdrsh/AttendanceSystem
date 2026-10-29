"""Reads the React pages of other roles need (found by the browser tests, tests/e2e)."""
from django.test import Client

from tests.core.fixtures import bearer


def test_attendance_admin_reads_branches_for_the_kiosk_setup_but_cannot_change_them(login_as, demo):
    # Attendance -> Kiosks ties each kiosk to a branch; its branch picker reads this list.
    att = login_as(['attendance_admin'])
    r = Client().get('/api/v1/settings/branches', **bearer(att.token))
    assert r.status_code == 200 and r.json()['branches'], r.json()
    w = Client().post('/api/v1/settings/branches', '{"code":"K1","nameEn":"Kiosk"}', content_type='application/json',
                      **bearer(att.token))
    assert (w.status_code, w.json()) == (403, {'error': 'requires role hr or admin'})
    for roles in (['payroll_admin'], ['manager'], ['employee']):
        r = Client().get('/api/v1/settings/branches', **bearer(login_as(roles).token))
        assert (r.status_code, r.json()) == (403, {'error': 'requires role hr or admin'}), roles


def test_branch_list_is_the_active_company_only(login_as, demo):
    from hajiri.db.models import Branch, Company
    other = Company.objects.create(code='OTHERB', name_en='Other')
    Branch.objects.create(company=other, code='OB', name_en='Other branch')
    att = login_as(['attendance_admin'])
    codes = {b['code'] for b in Client().get('/api/v1/settings/branches', **bearer(att.token)).json()['branches']}
    assert 'OB' not in codes
