"""live_punches() with a time window: the same punches as without one, a superseded punch stays hidden even when its
replacement lies just outside the window, and PostgreSQL reads (and locks) only the months involved instead of every
punch_event partition."""
import re

from hajiri.attendance import repo
from tests.core.punches import insert_punch
from tests.support.sql import execute


def _supersede(punch_id, minutes):
    """A correction row: the same punch moved by `minutes`, superseding the original."""
    execute("""INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time,
                 local_date, direction, verify_mode, source, idempotency_key, supersedes_id)
               SELECT device_id, device_sn, vendor, device_user_pin, employee_id, punched_at + make_interval(mins => %s),
                      device_local_time, local_date, direction, verify_mode, 'manual', idempotency_key || '-fix', id
               FROM punch_event WHERE id = %s""", [minutes, punch_id])


def test_a_window_returns_what_the_unbounded_query_returns(demo):
    sita = demo['sita']
    kept = insert_punch(sita, '2026-09-24', '10:00')
    late = insert_punch(sita, '2026-09-24', '18:30')
    _supersede(late, -30)
    # A punch near midnight whose correction lands on the next day, outside a one-day window.
    night = insert_punch(sita, '2026-09-24', '23:50')
    _supersede(night, 20)
    lo, hi = repo.local_dates_window('2026-09-24', '2026-09-24')
    windowed = set(map(str, repo.live_punches(lo, hi).filter(employee_id=sita, local_date='2026-09-24')
                       .values_list('id', flat=True)))
    unbounded = set(map(str, repo.live_punches().filter(employee_id=sita, local_date='2026-09-24')
                        .values_list('id', flat=True)))
    assert windowed == unbounded
    assert kept in windowed and late not in windowed and night not in windowed


def test_a_window_reads_only_the_months_it_covers(demo):
    lo, hi = repo.local_dates_window('2026-09-24', '2026-09-24')
    plan = repo.live_punches(lo, hi).filter(employee_id=demo['sita']).explain()
    months = set(re.findall(r'punch_event_\d{4}_\d{2}', plan))
    assert months and months <= {'punch_event_2026_08', 'punch_event_2026_09', 'punch_event_2026_10'}
