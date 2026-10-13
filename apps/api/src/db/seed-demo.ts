import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadConfig } from '../config.js';
import { createPool, type Db } from './pool.js';

/** Development seed: one company, one branch, a general shift, two employees, one pending ZKTeco device mapped to them. */
export async function seedDemo(db: Db): Promise<void> {
  await db.query(`
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
  `);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const db = createPool(loadConfig().DATABASE_URL);
  seedDemo(db).then(() => { console.log('demo data seeded'); return db.end(); })
    .catch((e) => { console.error(e.message); process.exit(1); });
}
