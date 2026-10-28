"""The demo seeder (port of apps/api/src/db/demo-suite and the CLI's seed-demo-suite safety rules).

The TS code has no test for it; these check that it fills every module of a fresh database, that a second run adds
nothing, and that it refuses to run without an explicit target or against the live database named in the env."""
import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from demo.management.commands import seed_demo_suite as cmd_mod
from demo.management.commands.seed_demo_suite import Command, NEED_TARGET, REFUSE_LIVE, is_live, live_database_urls
from demo.suite import DEMO_SUITE_SECTIONS, seed_demo_suite

from .sql import count, execute, one, rows


def test_fills_every_module_of_a_fresh_database(suite_company):
    cid = suite_company['company_id']
    logs: list[str] = []
    out = seed_demo_suite(company='DEMO', log=logs.append)
    assert out.failed == [], '\n'.join(f"{f['section']}: {f['error']}" for f in out.failed)
    assert out.done == DEMO_SUITE_SECTIONS
    assert out.skipped == []
    # Every employee got an account; the managers got the manager role.
    assert count('SELECT 1 FROM employee e WHERE e.company_id = %s AND NOT EXISTS (SELECT 1 FROM app_user u WHERE u.employee_id = e.id)', [cid]) == 0
    sita = suite_company['employees']['E001']
    assert one("SELECT 'manager' = ANY(roles) AS m FROM app_user WHERE employee_id = %s", [sita])['m'] is True
    # A sample of what each section leaves behind.
    checks = {
        "SELECT 1 FROM org_unit WHERE company_id = %s AND code IN ('PLAT', 'PROD')": 2,
        'SELECT 1 FROM designation WHERE company_id = %s': 14,
        'SELECT 1 FROM employee_document WHERE company_id = %s': None,
        'SELECT 1 FROM company_policy WHERE company_id = %s': 2,
        'SELECT 1 FROM asset WHERE company_id = %s': None,
        'SELECT 1 FROM employee_exit WHERE company_id = %s': 1,
        'SELECT 1 FROM helpdesk_ticket WHERE company_id = %s': 5,
        'SELECT 1 FROM timesheet WHERE company_id = %s': None,
        'SELECT 1 FROM kiosk WHERE company_id = %s': 1,
        'SELECT 1 FROM field_visit WHERE company_id = %s': None,
        'SELECT 1 FROM work_break WHERE company_id = %s': None,
        "SELECT 1 FROM shift WHERE company_id = %s AND code IN ('EARLY', 'LATE')": 2,
        'SELECT 1 FROM expense_claim WHERE company_id = %s': 7,
        'SELECT 1 FROM travel_request WHERE company_id = %s': 2,
        'SELECT 1 FROM tax_declaration_request WHERE company_id = %s': 2,
        'SELECT 1 FROM perf_goal WHERE company_id = %s': None,
        'SELECT 1 FROM perf_journal WHERE company_id = %s': None,
        'SELECT 1 FROM lrn_course WHERE company_id = %s': 4,
        'SELECT 1 FROM hiring_offer WHERE company_id = %s': 1,
        'SELECT 1 FROM announcement WHERE company_id = %s': 3,
        'SELECT 1 FROM suggestion WHERE company_id = %s': 2,
    }
    for sql, want in checks.items():
        n = count(sql, [cid])
        assert n == want if want is not None else n > 0, (sql, n)
    assert one("SELECT value FROM company_setting WHERE company_id = %s AND key = 'demo.suite'", [cid])['value'] == {'done': DEMO_SUITE_SECTIONS}
    # The seeder's sessions are revoked when it finishes.
    assert count("SELECT 1 FROM session WHERE user_agent = 'demo-suite seeder' AND revoked_at IS NULL") == 0
    assert any(m == '  ✓ engage' for m in logs)

    # A second run finds every section done and adds nothing.
    before = count('SELECT 1 FROM audit_log')
    again = seed_demo_suite(company='DEMO', log=lambda m: None)
    assert again.done == [] and again.failed == [] and again.skipped == DEMO_SUITE_SECTIONS
    assert count('SELECT 1 FROM helpdesk_ticket WHERE company_id = %s', [cid]) == 5
    # The second run wrote nothing at all.
    assert count('SELECT 1 FROM audit_log') == before


def test_records_go_through_the_modules_rules(suite_company):
    """Sample of the side effects the services apply: workflows, verification states, letters, notifications."""
    cid = suite_company['company_id']
    out = seed_demo_suite(company='DEMO', log=lambda m: None)
    assert out.failed == []
    # HR uploads count as verified; the three PAN cards employees uploaded wait for HR.
    assert count("SELECT 1 FROM employee_document WHERE company_id = %s AND status = 'pending'", [cid]) == 3
    # Four letters issued (three appointment letters and a salary certificate) and two requests waiting in Approvals.
    assert count('SELECT 1 FROM issued_letter WHERE company_id = %s', [cid]) == 4
    assert count("SELECT 1 FROM letter_request WHERE company_id = %s AND status = 'pending'", [cid]) == 2
    assert count("""SELECT 1 FROM workflow_instance WHERE company_id = %s AND object_type = 'letter_request' AND state = 'pending'""", [cid]) == 2
    # 60% of the team acknowledged the handbook.
    assert count("""SELECT 1 FROM policy_ack k JOIN company_policy p ON p.id = k.policy_id
                     WHERE p.company_id = %s AND p.title = 'Employee Handbook 2083'""", [cid]) == 9
    # Career: two confirmations, a grade change, a transfer and a promotion scheduled for next month.
    kinds = sorted(r['kind'] for r in rows('SELECT kind FROM career_event WHERE company_id = %s', [cid]))
    assert kinds == ['confirmation', 'confirmation', 'grade_change', 'promotion', 'transfer']
    assert one("SELECT status FROM career_event WHERE company_id = %s AND kind = 'promotion'", [cid])['status'] == 'scheduled'
    # Timesheets: two weeks submitted per tracker; the older week approved by the line manager.
    assert count("SELECT 1 FROM timesheet WHERE company_id = %s AND status = 'approved'", [cid]) > 0
    assert count("SELECT 1 FROM timesheet WHERE company_id = %s AND status = 'submitted'", [cid]) > 0
    # Expenses: the rejected claim and the draft keep their states.
    assert one("SELECT status FROM expense_claim WHERE company_id = %s AND title = 'Team lunch (not reimbursable)'", [cid])['status'] == 'rejected'
    assert one("SELECT status FROM expense_claim WHERE company_id = %s AND title = 'Conference ticket'", [cid])['status'] == 'draft'
    # Hiring: the rejected candidate, the one with an offer, two published jobs.
    assert count("SELECT 1 FROM hiring_job WHERE company_id = %s AND status = 'open'", [cid]) == 2
    assert count("SELECT 1 FROM hiring_scorecard s JOIN hiring_interview i ON i.id = s.interview_id WHERE i.company_id = %s", [cid]) == 2
    # Engage: the pulse survey has twelve answers.
    assert count("""SELECT 1 FROM survey_response r JOIN survey s ON s.id = r.survey_id WHERE s.company_id = %s AND s.title = 'Monthly pulse: Ashwin'""",
                 [cid]) == 12
    # Everyone the handbook went to was notified.
    assert count("SELECT 1 FROM notification_outbox WHERE company_id = %s AND template = 'policy.published'", [cid]) > 0


def test_only_runs_the_named_sections(suite_company):
    out = seed_demo_suite(company='DEMO', only=['structure', 'helpdesk'], log=lambda m: None)
    assert out.done == ['structure', 'helpdesk'] and out.failed == []
    assert count('SELECT 1 FROM perf_goal WHERE company_id = %s', [suite_company['company_id']]) == 0
    # Later runs pick up the rest.
    rest = seed_demo_suite(company='DEMO', only=['helpdesk', 'engage'], log=lambda m: None)
    assert rest.skipped == ['helpdesk'] and rest.done == ['engage']


def test_a_failed_section_is_reported_and_leaves_nothing(suite_company):
    cid = suite_company['company_id']
    execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'features', '{"off": ["time"]}')""", [cid])
    from hajiri.features.service import invalidate_features
    invalidate_features()
    out = seed_demo_suite(company='DEMO', only=['time', 'helpdesk'], log=lambda m: None)
    assert out.done == ['helpdesk']
    assert [f['section'] for f in out.failed] == ['time']
    assert 'feature off' in out.failed[0]['error']
    assert count('SELECT 1 FROM time_client WHERE company_id = %s', [cid]) == 0
    assert one("SELECT value FROM company_setting WHERE company_id = %s AND key = 'demo.suite'", [cid])['value'] == {'done': ['helpdesk']}


def test_field_work_off_stops_the_field_section_but_not_the_roster(suite_company):
    # The TS field gate covers /field/breaks, /visits, /kiosks, /kiosk-pin and /kiosk-qr only: the roster section
    # (/shifts and /field/swaps) still ran with field work switched off.
    from hajiri.features.service import invalidate_features
    cid = suite_company['company_id']
    execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'features', '{"off": ["field"]}')""", [cid])
    invalidate_features()
    out = seed_demo_suite(company='DEMO', only=['field', 'roster'], log=lambda m: None)
    assert out.done == ['roster']
    assert [f['section'] for f in out.failed] == ['field'] and 'feature off' in out.failed[0]['error']
    assert count('SELECT 1 FROM kiosk WHERE company_id = %s', [cid]) == 0
    assert count("SELECT 1 FROM shift WHERE company_id = %s AND code IN ('EARLY', 'LATE')", [cid]) == 2
    assert count('SELECT 1 FROM shift_swap WHERE company_id = %s', [cid]) == 1


def test_half_seeded_section_is_refused(suite_company):
    cid = suite_company['company_id']
    execute("INSERT INTO time_client (company_id, name) VALUES (%s, 'Left over')", [cid])
    out = seed_demo_suite(company='DEMO', only=['time'], log=lambda m: None)
    assert out.failed == [{'section': 'time', 'error': 'the time data is only partly there from an earlier run; restore the demo database '
                                                       '(or remove that data) and run again'}]


def test_needs_eight_active_employees_and_an_hr_user(demo):
    with pytest.raises(RuntimeError, match='needs an active HR or admin user'):
        seed_demo_suite(company='DEMO', log=lambda m: None)
    from hajiri.core import auth
    auth.create_user(company_id=demo['company_id'], email='hr@demo.test', name='HR', password='correct-horse-battery-staple', roles=['hr'])
    with pytest.raises(RuntimeError, match=r'needs at least 8 active employees \(found 2\)'):
        seed_demo_suite(company='DEMO', log=lambda m: None)
    with pytest.raises(RuntimeError, match='company NOPE not found'):
        seed_demo_suite(company='NOPE', log=lambda m: None)


# ------------------------------------------------------------------ the command and its safety rules

LIVE = 'postgres://hajiri:secret@localhost:5433/hajiri'


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    """The live database named in an env file (as server/.env names it), and nothing in the process environment."""
    f = tmp_path / '.env'
    f.write_text(f'PORT=3000\nDATABASE_URL={LIVE}\n')
    monkeypatch.setattr(cmd_mod, 'live_env_files', lambda: [f])
    monkeypatch.delenv('DATABASE_URL', raising=False)
    return f


def run(**opts) -> str:
    out = io.StringIO()
    call_command(Command(), stdout=out, **opts)
    return out.getvalue()


def test_command_needs_an_explicit_database_and_yes(live_env):
    with pytest.raises(CommandError) as e:
        run()
    assert str(e.value) == NEED_TARGET
    with pytest.raises(CommandError) as e:
        run(database='postgres://localhost:5433/hajiri_demo')
    assert str(e.value) == NEED_TARGET
    with pytest.raises(CommandError) as e:
        run(yes=True)
    assert str(e.value) == NEED_TARGET


@pytest.mark.parametrize('target', [
    LIVE, LIVE + '/', LIVE.upper().replace('POSTGRES://', 'postgres://'), 'postgres://localhost:5433/hajiri',
    'postgresql://other:pw@127.0.0.1:5433/hajiri', 'postgres://hajiri@localhost:5433/hajiri?sslmode=disable',
])
def test_command_refuses_the_live_database(live_env, target, suite_company):
    with pytest.raises(CommandError) as e:
        run(database=target, yes=True)
    assert str(e.value) == REFUSE_LIVE
    # Refused before anything was touched.
    assert one("SELECT 1 AS x FROM company_setting WHERE key = 'demo.suite'") is None
    assert count("SELECT 1 FROM session WHERE user_agent = 'demo-suite seeder'") == 0


def test_command_refuses_the_database_in_the_process_environment(live_env, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgres://localhost:5432/hajiri_prod')
    with pytest.raises(CommandError) as e:
        run(database='postgres://localhost/hajiri_prod', yes=True)
    assert str(e.value) == REFUSE_LIVE


def test_command_refuses_the_live_database_this_install_names():
    """Whatever this machine's env files and environment name as the live database is refused, unchanged."""
    urls = live_database_urls()
    if not urls:
        pytest.skip('no DATABASE_URL configured on this machine')
    for url in urls:
        with pytest.raises(CommandError) as e:
            run(database=url, yes=True)
        assert str(e.value) == REFUSE_LIVE


def test_is_live_compares_host_port_and_name():
    assert is_live('postgres://localhost:5433/hajiri', [LIVE])
    assert not is_live('postgres://localhost:5433/hajiri_demo', [LIVE])
    assert not is_live('postgres://localhost:5432/hajiri', [LIVE])
    assert not is_live('postgres://db.example.com:5433/hajiri', [LIVE])
    assert is_live('postgres://localhost/x', ['postgres://localhost:5432/x'])
    assert not is_live('postgres://localhost/x', [])
    with pytest.raises(CommandError, match='must be a postgres:// URL'):
        is_live('mysql://localhost/x', [LIVE])


def _this_database_url() -> str:
    s = connection.settings_dict
    auth = f"{s['USER']}@" if s['USER'] else ''
    return f"postgres://{auth}{s['HOST'] or 'localhost'}:{s['PORT'] or 5432}/{s['NAME']}"


def test_command_fills_a_demo_database(live_env, suite_company):
    text = run(database=_this_database_url(), yes=True, company='DEMO', only='structure,helpdesk')
    assert '  ✓ structure' in text and '  ✓ helpdesk' in text
    assert text.strip().endswith('done: structure, helpdesk; already there: none')
    text = run(database=_this_database_url(), yes=True, only='helpdesk')
    assert text.strip().endswith('done: nothing new; already there: helpdesk')


def test_command_reports_failed_sections(live_env, suite_company):
    execute("INSERT INTO time_client (company_id, name) VALUES (%s, 'Left over')", [suite_company['company_id']])
    with pytest.raises(CommandError) as e:
        run(database=_this_database_url(), yes=True, only='time')
    assert str(e.value) == 'failed: time'


def test_command_reports_a_company_that_cannot_be_seeded(live_env, demo):
    with pytest.raises(CommandError) as e:
        run(database=_this_database_url(), yes=True)
    assert str(e.value) == 'the company needs an active HR or admin user (who has set their password) to run the demo suite'
