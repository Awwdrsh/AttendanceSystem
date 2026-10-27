"""
Leave accrual (port of apps/api/src/modules/leave/accrual.ts, plan §10.6, Labour Act Sec 42-44, 49):

- worked_days basis (home leave): worked days / N, fractions carried in the numeric ledger;
- monthly basis (sick leave): accrual_days per month; nothing for a month the employee joined part-way (Sec 44 proviso);
- annual basis (casual): allocated once per fiscal year, pro-rated for joiners (allocate_annual_leave);
- caps: balance above max_balance is expired (and HR is told, so it can be paid out under Sec 49(3) if encashable);
- substitute leave past expires_on lapses.
Every posting is idempotent.
"""
from __future__ import annotations

from datetime import date

from engines.bs_calendar import ad_to_bs, bs_month_bounds, fiscal_year, fiscal_year_of

from hajiri.core.db import execute, one, rows
from hajiri.notifications.service import notify, user_ids_with_role

from .service import _type, fixed, iso, num, round2

FY_START_MONTH = 4  # Shrawan


def run_monthly_accrual(company_id: str, bs_year: int, bs_month: int) -> dict:
    """Accrue one BS month for a company: {'employees', 'postings', 'expired', 'bsYear', 'bsMonth'}."""
    company_id = str(company_id)
    bounds = bs_month_bounds(bs_year, bs_month)
    start_ad, end_ad = bounds['startAd'], bounds['endAd']
    types = [_type(t) for t in rows("SELECT * FROM leave_type WHERE company_id = %s AND accrual_basis <> 'none'", [company_id])]
    emps = rows("""SELECT id, gender, join_date FROM employee WHERE company_id = %s AND status = 'active'
                    AND (join_date IS NULL OR join_date <= %s::date)""", [company_id, end_ad])
    postings = 0

    def key(emp_id: str, type_id: str) -> str:
        return f'accrual:{emp_id}:{type_id}:{bs_year}-{bs_month}'

    for emp in emps:
        emp_id = str(emp['id'])
        join = iso(emp['join_date'])
        worked = one("""SELECT coalesce(sum(CASE WHEN status IN ('Present', 'WFH') THEN 1 WHEN status IN ('HalfDay', 'HalfDayLeave') THEN 0.5 ELSE 0 END), 0)::text AS worked
                          FROM v_attendance_latest WHERE employee_id = %s AND local_date BETWEEN %s AND %s""", [emp_id, start_ad, end_ad])
        worked_days = num(worked['worked'])
        for t in types:
            if t['gender_scope'] != 'all' and t['gender_scope'] != emp['gender']:
                continue
            if one("""SELECT 1 AS x FROM leave_ledger WHERE employee_id = %s AND leave_type_id = %s AND reason IN ('accrual', 'allocation')
                        AND note = %s""", [emp_id, t['id'], key(emp_id, t['id'])]):
                continue
            delta = 0.0
            if t['accrual_basis'] == 'worked_days':
                per = t['accrual_per_worked_days'] if t['accrual_per_worked_days'] is not None else 20
                delta = round2((worked_days / per) * num(t['accrual_days']))
            elif t['accrual_basis'] == 'monthly':
                delta = num(t['accrual_days'])
                # First year of service: a month counts once the employee was there for all of it.
                if join and join > start_ad:
                    delta = 0.0
            elif t['accrual_basis'] == 'annual':
                continue  # allocated per fiscal year by allocate_annual_leave below
            if delta <= 0:
                continue
            execute("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, note)
                       VALUES (%s, %s, %s, %s, 'accrual', 'accrual', %s)""", [emp_id, t['id'], end_ad, delta, key(emp_id, t['id'])])
            postings += 1
            # Cap (Sec 43: home <= 90, Sec 44: sick <= 45). Excess is expired; encashable excess is flagged to HR.
            if t['max_balance'] is not None:
                bal = one('SELECT coalesce(sum(delta_days), 0)::text AS b FROM leave_ledger WHERE employee_id = %s AND leave_type_id = %s',
                          [emp_id, t['id']])
                excess = num(bal['b']) - num(t['max_balance'])
                if excess > 0:
                    execute("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, note)
                               VALUES (%s, %s, %s, %s, 'expiry', 'accrual_cap', %s)""",
                            [emp_id, t['id'], end_ad, -excess, f"{key(emp_id, t['id'])}:cap"])
                    if t['encashable']:
                        for user_id in user_ids_with_role(company_id, 'hr'):
                            notify(company_id=company_id, user_id=user_id, employee_id=emp_id, template='leave.cap_exceeded',
                                   title='Leave balance above cap',
                                   body=f"{t['name_en']} balance exceeded {t['max_balance']} days by {fixed(excess, 2)}; "
                                        'excess should be paid out (Sec 49(3)).',
                                   payload={'leaveTypeId': t['id'], 'excess': excess})
    postings += allocate_annual_leave(company_id, end_ad)
    expired = expire_substitute_leave(company_id, end_ad)
    return {'employees': len(emps), 'postings': postings, 'expired': expired, 'bsYear': bs_year, 'bsMonth': bs_month}


def _months_since(from_ad: str, to_ad: str) -> int:
    a, b = date.fromisoformat(from_ad), date.fromisoformat(to_ad)
    return (b.year - a.year) * 12 + (b.month - a.month) + (1 if b.day > a.day else 0)


def allocate_annual_leave(company_id: str, as_of_ad: str, employee_id: str | None = None) -> int:
    """
    Annual-basis leave (casual): one allocation per employee per fiscal year, granted as soon as the employee is on the
    books. Joiners during the year get a share pro-rated by the months left. Any existing allocation of the type in the
    fiscal year (automatic or an HR opening allocation) counts, so this never grants twice. Returns postings made.
    """
    company_id = str(company_id)
    fy = fiscal_year_of(ad_to_bs(as_of_ad))
    next_fy_start = fiscal_year(fy.start_year + 1).start_ad
    types = [_type(t) for t in rows("SELECT * FROM leave_type WHERE company_id = %s AND accrual_basis = 'annual' AND accrual_days > 0",
                                    [company_id])]
    if not types:
        return 0
    emp_param = str(employee_id) if employee_id else None
    emps = rows("""SELECT id, gender, join_date::text FROM employee WHERE company_id = %s AND status = 'active'
                    AND (join_date IS NULL OR join_date <= %s::date) AND (exit_date IS NULL OR exit_date >= %s::date)
                    AND (%s::uuid IS NULL OR id = %s::uuid)""", [company_id, as_of_ad, fy.start_ad, emp_param, emp_param])
    n = 0
    for emp in emps:
        emp_id = str(emp['id'])
        for t in types:
            if t['gender_scope'] != 'all' and t['gender_scope'] != emp['gender']:
                continue
            if one("""SELECT 1 AS x FROM leave_ledger WHERE employee_id = %s AND leave_type_id = %s AND reason = 'allocation'
                        AND effective_date >= %s::date AND effective_date < %s::date LIMIT 1""",
                   [emp_id, t['id'], fy.start_ad, next_fy_start]):
                continue
            join = emp['join_date']
            joined_in_year = join is not None and join > fy.start_ad
            months = max(0, 12 - _months_since(fy.start_ad, join)) if joined_in_year else 12
            delta = round2(num(t['accrual_days']) * months / 12)
            if delta <= 0:
                continue
            note = f"allocation:{emp_id}:{t['id']}:FY{fy.start_year}"
            # Serialise concurrent callers (worker, HR catch-up, balance views) on the same employee and type.
            n += execute("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, note)
                            SELECT %s, %s, %s::date, %s, 'allocation', 'accrual', %s
                             WHERE pg_try_advisory_xact_lock(hashtext(%s)) AND NOT EXISTS (
                               SELECT 1 FROM leave_ledger WHERE employee_id = %s AND leave_type_id = %s AND reason = 'allocation'
                                  AND effective_date >= %s::date AND effective_date < %s::date)""",
                         [emp_id, t['id'], join if joined_in_year else fy.start_ad, delta, note, note, emp_id, t['id'],
                          fy.start_ad, next_fy_start])
    return n


def run_leave_catch_up(company_id: str, as_of_ad: str) -> dict:
    """
    Run every completed BS month of the current fiscal year that has not been accrued yet (each month is idempotent),
    then grant any missing annual allocation as of today: {'months': [summary], 'annualPostings', 'asOf'}.
    """
    bs = ad_to_bs(as_of_ad)
    fy = fiscal_year_of(bs)
    months = []
    y, m = fy.start_year, FY_START_MONTH
    while y < bs.year or (y == bs.year and m < bs.month):
        months.append(run_monthly_accrual(company_id, y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    annual = allocate_annual_leave(company_id, as_of_ad)
    return {'months': months, 'annualPostings': annual, 'asOf': as_of_ad}


def expire_substitute_leave(company_id: str, as_of: str) -> int:
    """Substitute leave not used within its window lapses (Sec 42: 21 days). Returns rows expired."""
    due = rows("""SELECT l.id, l.employee_id, l.leave_type_id, l.delta_days, l.expires_on FROM leave_ledger l
                   JOIN leave_type t ON t.id = l.leave_type_id
                  WHERE t.company_id = %s AND l.reason = 'comp_off' AND l.expires_on IS NOT NULL AND l.expires_on < %s::date
                    AND NOT EXISTS (SELECT 1 FROM leave_ledger x WHERE x.reason = 'expiry' AND x.ref_type = 'comp_off' AND x.ref_id = l.id)""",
               [str(company_id), as_of])
    n = 0
    for row in due:
        # Only the unused part of the credit lapses: the balance cannot go below zero because of expiry.
        bal = one('SELECT coalesce(sum(delta_days), 0)::text AS b FROM leave_ledger WHERE employee_id = %s AND leave_type_id = %s',
                  [row['employee_id'], row['leave_type_id']])
        lapse = min(num(row['delta_days']), max(0.0, num(bal['b'])))
        execute("""INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason, ref_type, ref_id, note)
                   VALUES (%s, %s, %s, %s, 'expiry', 'comp_off', %s, %s)""",
                [row['employee_id'], row['leave_type_id'], row['expires_on'], -lapse, row['id'],
                 f"substitute leave expired after {iso(row['expires_on'])}"])
        n += 1
    return n
