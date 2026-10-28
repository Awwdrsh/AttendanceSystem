"""Punch insertion helper used by the attendance and worker tests (insertPunch in the TS tests)."""
from engines.attendance import local_date_of, local_time_ms

from hajiri.attendance.repo import from_ms
from tests.support.sql import one

DEVICE = '00000000-0000-7000-8000-000000000021'


def insert_punch(employee_id, local_date, hhmm, pin='1', verify='fingerprint', source='zkteco', key_prefix='k'):
    at = local_time_ms(local_date, hhmm)
    device_local = f'{local_date} {hhmm}:00'
    r = one("""INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time,
                 local_date, direction, verify_mode, source, idempotency_key)
               VALUES (%s, 'DEMO-01', 'zkteco', %s, %s, %s, %s, %s, 'unknown', %s, %s, %s) RETURNING id""",
            [DEVICE, pin, employee_id, from_ms(at), device_local, local_date_of(at), verify, source,
             f'{key_prefix}-{pin}-{device_local}-{verify}-{source}'])
    return str(r['id'])
