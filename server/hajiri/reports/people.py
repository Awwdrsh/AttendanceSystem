"""
People record reports (port of reportCatalogRoutes in apps/api/src/modules/reports/catalog.ts). HR runs all of them;
a manager runs the two team reports, limited to their team. Each returns a Table; `?format=csv` gives Bikram Sambat
dates and neutralised formula cells (send_table).
"""
from __future__ import annotations

from datetime import date, timedelta

from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.clock import npt_today
from hajiri.core.db import rows
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError
from hajiri.core.schema import Enum, Int, IsoDate, Obj, Uuid, parse

from .csv import Table, _to_fixed, cols

PEOPLE_REPORTS = ('headcount-on-date', 'statistics', 'joiners-leavers', 'birthdays-anniversaries', 'nominees',
                  'emergency-contacts', 'addresses', 'education', 'licence-register', 'record-completeness')
# Reports a manager may run for their own team; the rest hold personal data and are HR only.
MANAGER_REPORTS = frozenset({'headcount-on-date', 'joiners-leavers'})

FILTERS = Obj({'format': Enum(('json', 'csv'), optional=True), 'branchId': Uuid(optional=True),
               'orgUnitId': Uuid(optional=True), 'status': Enum(('active', 'exited', 'all'), default='active')})


def _employee_where(user: AuthUser, f: dict, params: dict, as_of: str, *, dated: bool = False) -> str:
    """Company, status, branch and department (with sub-units) on `as_of`; for managers, their direct reports and
    themselves. Adds its parameters to `params` (%(company)s, %(asof)s, ...)."""
    params['company'] = company_id_of(user)
    params['asof'] = as_of
    parts = ['e.company_id = %(company)s']
    if not dated:
        if f['status'] == 'active':
            parts.append("e.status = 'active'")
        if f['status'] == 'exited':
            parts.append("e.status = 'exited'")
    if f.get('branchId'):
        params['branch'] = f['branchId']
        parts.append('EXISTS (SELECT 1 FROM employee_assignment fa WHERE fa.employee_id = e.id AND fa.valid @> %(asof)s::date '
                     'AND fa.branch_id = %(branch)s)')
    if f.get('orgUnitId'):
        params['unit'] = f['orgUnitId']
        parts.append("""EXISTS (SELECT 1 FROM employee_assignment fa WHERE fa.employee_id = e.id AND fa.valid @> %(asof)s::date AND fa.org_unit_id IN (
      WITH RECURSIVE sub AS (SELECT id FROM org_unit WHERE id = %(unit)s UNION ALL SELECT o.id FROM org_unit o JOIN sub ON o.parent_id = sub.id) SELECT id FROM sub))""")
    if not has_role(user, 'hr'):
        params['me'] = user.employee_id
        parts.append('(e.id = %(me)s::uuid OR EXISTS (SELECT 1 FROM employee_assignment ma WHERE ma.employee_id = e.id '
                     'AND ma.manager_id = %(me)s::uuid AND ma.valid @> %(asof)s::date))')
    return ' AND '.join(parts)


def _placement(as_of: str = '%(asof)s') -> str:
    return f"""
  LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> {as_of}::date
  LEFT JOIN branch b ON b.id = a.branch_id
  LEFT JOIN org_unit o ON o.id = a.org_unit_id
  LEFT JOIN employee m ON m.id = a.manager_id"""


def _address(col: str) -> str:
    return (f"nullif(concat_ws(', ', {col}->>'street', CASE WHEN {col}->>'ward' IS NOT NULL THEN 'Ward ' || ({col}->>'ward') END, "
            f"{col}->>'municipality', {col}->>'district', {col}->>'province'), '')")


def _check_access(user: AuthUser, name: str) -> None:
    if not has_role(user, 'hr', 'manager'):
        raise ForbiddenError('requires role hr or manager')
    if name not in PEOPLE_REPORTS:
        raise NotFoundError('not found')
    if not has_role(user, 'hr') and name not in MANAGER_REPORTS:
        raise ForbiddenError('this report is for HR only')


def people_report(user: AuthUser, name: str, query: dict) -> Table:
    _check_access(user, name)
    today = npt_today().isoformat()
    f = parse(FILTERS, {k: query[k] for k in ('format', 'branchId', 'orgUnitId', 'status') if k in query})
    p: dict = {}
    pl = _placement()

    if name == 'headcount-on-date':
        q = parse(Obj({'date': IsoDate(default=today)}), {k: query[k] for k in ('date',) if k in query})
        where = _employee_where(user, f, p, q['date'], dated=True)
        found = rows(f"""
            SELECT coalesce(b.name_en, '(no branch)') AS branch, coalesce(o.name_en, '(no department)') AS department,
                   coalesce(a.employment_type, '') AS employment_type, count(*)::int AS headcount,
                   count(*) FILTER (WHERE e.gender = 'female')::int AS female, count(*) FILTER (WHERE e.gender = 'male')::int AS male,
                   count(*) FILTER (WHERE e.gender IS NULL OR e.gender = 'other')::int AS other
              FROM employee e {pl}
             WHERE {where} AND (e.join_date IS NULL OR e.join_date <= %(asof)s::date) AND (e.exit_date IS NULL OR e.exit_date >= %(asof)s::date)
             GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""", p)
        return Table(cols(('branch', 'Branch'), ('department', 'Department'), ('employment_type', 'Employment type'),
                          ('headcount', 'Headcount', 'number'), ('female', 'Female', 'number'), ('male', 'Male', 'number'),
                          ('other', 'Other or not recorded', 'number')), found, f"headcount-{q['date']}")

    if name == 'statistics':
        where = _employee_where(user, f, p, today)
        t = '%(asof)s'
        found = rows(f"""
            WITH e AS (SELECT e.* FROM employee e WHERE {where}),
                 dims AS (
                   SELECT 'Age' AS dimension, CASE WHEN dob IS NULL THEN 'Not recorded' WHEN age({t}::date, dob) < interval '25 years' THEN 'Under 25'
                     WHEN age({t}::date, dob) < interval '35 years' THEN '25 to 34' WHEN age({t}::date, dob) < interval '45 years' THEN '35 to 44'
                     WHEN age({t}::date, dob) < interval '55 years' THEN '45 to 54' ELSE '55 and over' END AS bucket,
                     CASE WHEN dob IS NULL THEN 9 WHEN age({t}::date, dob) < interval '25 years' THEN 1 WHEN age({t}::date, dob) < interval '35 years' THEN 2
                     WHEN age({t}::date, dob) < interval '45 years' THEN 3 WHEN age({t}::date, dob) < interval '55 years' THEN 4 ELSE 5 END AS sort FROM e
                   UNION ALL
                   SELECT 'Length of service', CASE WHEN join_date IS NULL THEN 'Not recorded' WHEN age({t}::date, join_date) < interval '1 year' THEN 'Under 1 year'
                     WHEN age({t}::date, join_date) < interval '3 years' THEN '1 to 3 years' WHEN age({t}::date, join_date) < interval '5 years' THEN '3 to 5 years'
                     WHEN age({t}::date, join_date) < interval '10 years' THEN '5 to 10 years' ELSE '10 years or more' END,
                     CASE WHEN join_date IS NULL THEN 9 WHEN age({t}::date, join_date) < interval '1 year' THEN 1 WHEN age({t}::date, join_date) < interval '3 years' THEN 2
                     WHEN age({t}::date, join_date) < interval '5 years' THEN 3 WHEN age({t}::date, join_date) < interval '10 years' THEN 4 ELSE 5 END FROM e
                   UNION ALL
                   SELECT 'Gender', coalesce(initcap(gender), 'Not recorded'), CASE gender WHEN 'female' THEN 1 WHEN 'male' THEN 2 WHEN 'other' THEN 3 ELSE 9 END FROM e
                   UNION ALL
                   SELECT 'Marital status', CASE marital_status WHEN 'single' THEN 'Single' WHEN 'couple' THEN 'Married' ELSE 'Not recorded' END,
                     CASE marital_status WHEN 'single' THEN 1 WHEN 'couple' THEN 2 ELSE 9 END FROM e
                   UNION ALL
                   SELECT 'Province (permanent address)', coalesce(nullif(permanent_address->>'province', ''), 'Not recorded'),
                     CASE WHEN nullif(permanent_address->>'province', '') IS NULL THEN 9 ELSE 1 END FROM e
                   UNION ALL
                   SELECT 'District (permanent address)', coalesce(nullif(permanent_address->>'district', ''), 'Not recorded'),
                     CASE WHEN nullif(permanent_address->>'district', '') IS NULL THEN 9 ELSE 1 END FROM e)
            SELECT dimension, bucket, min(sort)::int AS sort, count(*)::int AS count FROM dims GROUP BY dimension, bucket
             ORDER BY CASE dimension WHEN 'Age' THEN 1 WHEN 'Length of service' THEN 2 WHEN 'Gender' THEN 3 WHEN 'Marital status' THEN 4
                      WHEN 'Province (permanent address)' THEN 5 ELSE 6 END, sort, count DESC, bucket""", p)
        totals: dict[str, int] = {}
        for x in found:
            totals[x['dimension']] = totals.get(x['dimension'], 0) + x['count']
        out = [{'dimension': x['dimension'], 'bucket': x['bucket'], 'count': x['count'],
                'share': f"{_to_fixed(x['count'] / totals[x['dimension']] * 100, 1)}%" if totals.get(x['dimension']) else ''} for x in found]
        return Table(cols(('dimension', 'Measure'), ('bucket', 'Group'), ('count', 'Employees', 'number'), ('share', 'Share')),
                     out, 'people-statistics')

    if name == 'joiners-leavers':
        q = parse(Obj({'from': IsoDate(), 'to': IsoDate()}), {k: query[k] for k in ('from', 'to') if k in query})
        if q['from'] > q['to']:
            raise BadRequestError('the start date is after the end date')
        where = _employee_where(user, {**f, 'status': 'all'}, p, today)
        p.update({'from': q['from'], 'to': q['to']})
        found = rows(f"""
            SELECT * FROM (
              SELECT 'Joined' AS movement, e.join_date AS date, e.emp_code, e.name_en AS name, e.designation, b.name_en AS branch,
                     o.name_en AS department, a.employment_type, NULL::text AS exit_kind
                FROM employee e {pl} WHERE {where} AND e.join_date BETWEEN %(from)s::date AND %(to)s::date
              UNION ALL
              SELECT 'Left', e.exit_date, e.emp_code, e.name_en, e.designation, b.name_en, o.name_en, a.employment_type,
                     (SELECT x.kind FROM employee_exit x WHERE x.employee_id = e.id AND x.status IN ('accepted', 'completed')
                       ORDER BY x.created_at DESC LIMIT 1)
                FROM employee e {pl} WHERE {where} AND e.exit_date BETWEEN %(from)s::date AND %(to)s::date) x
             ORDER BY date, emp_code""", p)
        out = [{**x, 'exit_kind': str(x['exit_kind']).replace('_', ' ', 1) if x['exit_kind'] else ''} for x in found]
        return Table(cols(('movement', 'Movement'), ('date', 'Date', 'date'), ('emp_code', 'Emp code'), ('name', 'Name'),
                          ('designation', 'Designation'), ('branch', 'Branch'), ('department', 'Department'),
                          ('employment_type', 'Employment type'), ('exit_kind', 'Reason for leaving')),
                     out, f"joiners-leavers-{q['from']}_{q['to']}")

    if name == 'birthdays-anniversaries':
        q = parse(Obj({'from': IsoDate(default=today), 'days': Int(min=1, max=92, default=30, coerce=True)}),
                  {k: query[k] for k in ('from', 'days') if k in query})
        where = _employee_where(user, {**f, 'status': 'active'}, p, today)
        p.update({'from': q['from'], 'days': q['days']})
        # Each day of the window, matched against the month and day of the birth and joining dates (29 Feb -> 28 Feb).
        found = rows(f"""
            WITH days AS (SELECT (%(from)s::date + g)::date AS d FROM generate_series(0, %(days)s::int - 1) g)
            SELECT * FROM (
              SELECT 'Birthday' AS occasion, days.d AS date, e.emp_code, e.name_en AS name, o.name_en AS department, b.name_en AS branch,
                     (extract(year FROM days.d) - extract(year FROM e.dob))::int AS years
                FROM employee e {pl} JOIN days ON e.dob IS NOT NULL
                 AND to_char(days.d, 'MM-DD') = CASE WHEN to_char(e.dob, 'MM-DD') = '02-29' AND NOT (extract(year FROM days.d)::int %% 4 = 0) THEN '02-28' ELSE to_char(e.dob, 'MM-DD') END
               WHERE {where}
              UNION ALL
              SELECT 'Work anniversary', days.d, e.emp_code, e.name_en, o.name_en, b.name_en, (extract(year FROM days.d) - extract(year FROM e.join_date))::int
                FROM employee e {pl} JOIN days ON e.join_date IS NOT NULL AND e.join_date < days.d
                 AND to_char(days.d, 'MM-DD') = CASE WHEN to_char(e.join_date, 'MM-DD') = '02-29' AND NOT (extract(year FROM days.d)::int %% 4 = 0) THEN '02-28' ELSE to_char(e.join_date, 'MM-DD') END
               WHERE {where}) x
             ORDER BY date, occasion, name""", p)
        return Table(cols(('date', 'Date', 'date'), ('occasion', 'Occasion'), ('emp_code', 'Emp code'), ('name', 'Name'),
                          ('department', 'Department'), ('branch', 'Branch'), ('years', 'Years', 'number')),
                     found, f"birthdays-anniversaries-{q['from']}")

    if name == 'nominees':
        where = _employee_where(user, f, p, today)
        found = rows(f"""
            SELECT e.emp_code, e.name_en AS employee, o.name_en AS department, fm.name AS nominee,
                   replace(fm.relation, '_', ' ') AS relation, fm.dob, fm.nominee_share::text AS share, fm.phone
              FROM employee e {pl} JOIN employee_family fm ON fm.employee_id = e.id AND fm.is_nominee
             WHERE {where} ORDER BY e.emp_code, fm.sort, fm.name""", p)
        return Table(cols(('emp_code', 'Emp code'), ('employee', 'Employee'), ('department', 'Department'), ('nominee', 'Nominee'),
                          ('relation', 'Relation'), ('dob', 'Date of birth', 'date'), ('share', 'Share (%)'), ('phone', 'Phone')),
                     found, 'nominees')

    if name == 'emergency-contacts':
        where = _employee_where(user, f, p, today)
        found = rows(f"""
            SELECT e.emp_code, e.name_en AS employee, o.name_en AS department, e.blood_group, c.name AS contact, c.relation,
                   c.phone, c.alt_phone, c.address
              FROM employee e {pl} JOIN employee_emergency_contact c ON c.employee_id = e.id
             WHERE {where} ORDER BY e.emp_code, c.sort, c.name""", p)
        return Table(cols(('emp_code', 'Emp code'), ('employee', 'Employee'), ('department', 'Department'),
                          ('blood_group', 'Blood group'), ('contact', 'Contact'), ('relation', 'Relation'), ('phone', 'Phone'),
                          ('alt_phone', 'Other phone'), ('address', 'Address')), found, 'emergency-contacts')

    if name == 'addresses':
        where = _employee_where(user, f, p, today)
        found = rows(f"""
            SELECT e.emp_code, e.name_en AS employee, o.name_en AS department, b.name_en AS branch,
                   e.permanent_address->>'province' AS province, e.permanent_address->>'district' AS district,
                   {_address('e.permanent_address')} AS permanent_address, {_address('e.temporary_address')} AS temporary_address
              FROM employee e {pl} WHERE {where} ORDER BY e.emp_code""", p)
        return Table(cols(('emp_code', 'Emp code'), ('employee', 'Employee'), ('department', 'Department'), ('branch', 'Branch'),
                          ('province', 'Province'), ('district', 'District'), ('permanent_address', 'Permanent address'),
                          ('temporary_address', 'Temporary address')), found, 'addresses')

    if name == 'education':
        where = _employee_where(user, f, p, today)
        found = rows(f"""
            SELECT e.emp_code, e.name_en AS employee, o.name_en AS department, ed.level, ed.degree, ed.field, ed.institution,
                   ed.board, ed.start_year, ed.end_year, ed.result
              FROM employee e {pl} JOIN employee_education ed ON ed.employee_id = e.id
             WHERE {where} ORDER BY e.emp_code, ed.end_year DESC NULLS LAST, ed.sort""", p)
        level = {'see': 'SEE', 'plus_two': '+2', 'diploma': 'Diploma', 'bachelor': 'Bachelor', 'master': 'Master',
                 'mphil': 'MPhil', 'phd': 'PhD', 'other': 'Other'}
        out = [{**x, 'level': level.get(str(x['level']), x['level'])} for x in found]
        return Table(cols(('emp_code', 'Emp code'), ('employee', 'Employee'), ('department', 'Department'), ('level', 'Level'),
                          ('degree', 'Degree'), ('field', 'Field'), ('institution', 'Institution'),
                          ('board', 'Board or university'), ('start_year', 'From', 'number'), ('end_year', 'To', 'number'),
                          ('result', 'Result')), out, 'education')

    if name == 'licence-register':
        q = parse(Obj({'withinDays': Int(min=0, max=3650, default=90, coerce=True)}),
                  {k: query[k] for k in ('withinDays',) if k in query})
        where = _employee_where(user, f, p, today)
        # ID numbers are encrypted; the register shows only whether one is on file, never the number.
        found = rows(f"""
            SELECT * FROM (
              SELECT e.emp_code, e.name_en AS employee, o.name_en AS department, 'Passport' AS document,
                     e.passport_no_enc IS NOT NULL AS on_file, e.passport_expiry AS expiry, NULL::text AS detail
                FROM employee e {pl} WHERE {where} AND (e.passport_no_enc IS NOT NULL OR e.passport_expiry IS NOT NULL)
              UNION ALL
              SELECT e.emp_code, e.name_en, o.name_en, 'Driving licence', e.licence_no_enc IS NOT NULL, e.licence_expiry, NULL
                FROM employee e {pl} WHERE {where} AND (e.licence_no_enc IS NOT NULL OR e.licence_expiry IS NOT NULL)
              UNION ALL
              SELECT e.emp_code, e.name_en, o.name_en, 'Citizenship', true, NULL::date,
                     nullif(concat_ws(', ', e.citizenship_district, CASE WHEN e.citizenship_issued_on IS NOT NULL THEN 'issued' END), '')
                FROM employee e {pl} WHERE {where} AND e.citizenship_no_enc IS NOT NULL
              UNION ALL
              SELECT e.emp_code, e.name_en, o.name_en, dc.name || ': ' || d.title, true, d.expiry_date,
                     CASE d.status WHEN 'verified' THEN 'Verified' WHEN 'pending' THEN 'Waiting for HR' ELSE 'Rejected' END
                FROM employee e {pl} JOIN employee_document d ON d.employee_id = e.id AND d.is_current AND d.deleted_at IS NULL
                 AND d.expiry_date IS NOT NULL
                JOIN document_category dc ON dc.id = d.category_id
               WHERE {where}) x
             ORDER BY expiry NULLS LAST, emp_code""", p)
        lim = (date.fromisoformat(today) + timedelta(days=q['withinDays'])).isoformat()

        def state(exp):
            if not exp:
                return 'No expiry'
            e = exp.isoformat() if hasattr(exp, 'isoformat') else str(exp)
            return 'Expired' if e < today else 'Expiring soon' if e <= lim else 'Valid'
        out = [{**x, 'on_file': 'Recorded' if x['on_file'] else 'Not recorded', 'state': state(x['expiry'])} for x in found]
        return Table(cols(('emp_code', 'Emp code'), ('employee', 'Employee'), ('department', 'Department'),
                          ('document', 'Document'), ('on_file', 'Number'), ('detail', 'Detail'), ('expiry', 'Expires', 'date'),
                          ('state', 'State')), out, 'licence-register')

    # record-completeness
    where = _employee_where(user, f, p, today)
    found = rows(f"""
        SELECT e.emp_code, e.name_en AS employee, o.name_en AS department,
               jsonb_build_object(
                 'Date of birth', e.dob IS NOT NULL, 'Gender', e.gender IS NOT NULL, 'Marital status', e.marital_status IS NOT NULL,
                 'Phone', coalesce(e.phone, e.personal_phone) IS NOT NULL, 'Personal email', e.personal_email IS NOT NULL,
                 'Permanent address', nullif(e.permanent_address->>'district', '') IS NOT NULL, 'Citizenship', e.citizenship_no_enc IS NOT NULL,
                 'PAN', e.pan_enc IS NOT NULL, 'Bank account', e.bank_account_enc IS NOT NULL,
                 'SSF ID', e.regime <> 'ssf' OR nullif(e.ssf_id, '') IS NOT NULL, 'Designation', nullif(e.designation, '') IS NOT NULL,
                 'Department', a.org_unit_id IS NOT NULL, 'Manager', a.manager_id IS NOT NULL,
                 'Emergency contact', EXISTS (SELECT 1 FROM employee_emergency_contact c WHERE c.employee_id = e.id),
                 'Family', EXISTS (SELECT 1 FROM employee_family fm WHERE fm.employee_id = e.id),
                 'Education', EXISTS (SELECT 1 FROM employee_education ed WHERE ed.employee_id = e.id),
                 'Photo', e.avatar_file_id IS NOT NULL) AS checks
          FROM employee e {pl} WHERE {where} ORDER BY e.emp_code""", p)
    out = []
    for x in found:
        checks = x.pop('checks')
        done = sum(1 for v in checks.values() if v)
        out.append({**x, 'complete': _js_round(done / len(checks) * 100),
                    'missing': ', '.join(k for k, v in checks.items() if not v)})
    out.sort(key=lambda r: (r['complete'], str(r['emp_code'])))
    return Table(cols(('emp_code', 'Emp code'), ('employee', 'Employee'), ('department', 'Department'),
                      ('complete', 'Complete (%)', 'number'), ('missing', 'Missing')), out, 'record-completeness')


def _js_round(x: float) -> int:
    import math
    return math.floor(x + 0.5)
