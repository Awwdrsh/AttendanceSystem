"""Development seed (port of apps/api/src/db/seed-demo.ts): company DEMO, one branch, a general 10-18 shift, two
employees and one ZKTeco device mapped to them. The integration tests build on it, with the same fixed ids."""
from __future__ import annotations

from django.db import transaction

from hajiri.core.bs_calendar_seed import seed_bs_calendar
from hajiri.core.db import execute

from .seed import seed_defaults

DEMO = {
    'company_id': '00000000-0000-7000-8000-000000000001',
    'branch_id': '00000000-0000-7000-8000-000000000002',
    'shift_id': '00000000-0000-7000-8000-000000000003',
    'sita': '00000000-0000-7000-8000-000000000011',
    'ram': '00000000-0000-7000-8000-000000000012',
    'device_id': '00000000-0000-7000-8000-000000000021',
}

_SQL = """
INSERT INTO company (id, code, name_en, name_ne) VALUES ('00000000-0000-7000-8000-000000000001', 'DEMO', 'Demo Company', 'डेमो कम्पनी')
  ON CONFLICT (code) DO NOTHING;
INSERT INTO branch (id, company_id, code, name_en) VALUES ('00000000-0000-7000-8000-000000000002', '00000000-0000-7000-8000-000000000001', 'HQ', 'Head Office')
  ON CONFLICT (company_id, code) DO NOTHING;
INSERT INTO shift (id, company_id, code, name_en, start_time, end_time, grace_in_min, early_in_window_min, late_out_window_min, half_day_threshold_min, absent_threshold_min)
  VALUES ('00000000-0000-7000-8000-000000000003', '00000000-0000-7000-8000-000000000001', 'GEN', 'General 10-18', '10:00', '18:00', 15, 60, 120, 240, 120)
  ON CONFLICT (company_id, code) DO NOTHING;
INSERT INTO employee (id, company_id, emp_code, name_en, name_ne, gender)
  VALUES ('00000000-0000-7000-8000-000000000011', '00000000-0000-7000-8000-000000000001', 'E001', 'Sita Sharma', 'सीता शर्मा', 'female'),
         ('00000000-0000-7000-8000-000000000012', '00000000-0000-7000-8000-000000000001', 'E002', 'Ram Thapa', 'राम थापा', 'male')
  ON CONFLICT (company_id, emp_code) DO NOTHING;
INSERT INTO employee_assignment (employee_id, valid, branch_id, employment_type)
  VALUES ('00000000-0000-7000-8000-000000000011', daterange('2026-01-01', NULL), '00000000-0000-7000-8000-000000000002', 'regular'),
         ('00000000-0000-7000-8000-000000000012', daterange('2026-01-01', NULL), '00000000-0000-7000-8000-000000000002', 'regular')
  ON CONFLICT DO NOTHING;
INSERT INTO shift_assignment (employee_id, shift_id, valid)
  VALUES ('00000000-0000-7000-8000-000000000011', '00000000-0000-7000-8000-000000000003', daterange('2026-01-01', NULL)),
         ('00000000-0000-7000-8000-000000000012', '00000000-0000-7000-8000-000000000003', daterange('2026-01-01', NULL))
  ON CONFLICT DO NOTHING;
INSERT INTO device (id, company_id, branch_id, serial_no, vendor, protocol, name, status)
  VALUES ('00000000-0000-7000-8000-000000000021', '00000000-0000-7000-8000-000000000001', '00000000-0000-7000-8000-000000000002', 'DEMO-01', 'zkteco', 'adms', 'Front door (simulator)', 'active')
  ON CONFLICT (serial_no) DO NOTHING;
INSERT INTO device_user_map (device_id, device_user_pin, employee_id, valid)
  VALUES ('00000000-0000-7000-8000-000000000021', '1', '00000000-0000-7000-8000-000000000011', daterange('2026-01-01', NULL)),
         ('00000000-0000-7000-8000-000000000021', '2', '00000000-0000-7000-8000-000000000012', daterange('2026-01-01', NULL))
  ON CONFLICT DO NOTHING;
"""


@transaction.atomic
def seed_demo(*, calendar: bool = True, defaults: bool = True) -> dict:
    for stmt in (s.strip() for s in _SQL.split(';\n') if s.strip()):
        execute(stmt)
    out: dict = {}
    if calendar:
        out['bsCalendar'] = seed_bs_calendar()
    if defaults:
        out['defaults'] = seed_defaults(DEMO['company_id'])
    return out
