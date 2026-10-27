"""Field-level history of the employee record (port of apps/api/src/modules/people/history.ts)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from hajiri.core.db import execute, one

SOURCES = ('hr_edit', 'change_request', 'career_event', 'import', 'system')


def change(field: str, label: str, from_: str | None, to: str | None) -> dict:
    """A change as the TS code passes it around: {field, label, from, to}."""
    return {'field': field, 'label': label, 'from': from_, 'to': to}


def record_field_changes(*, company_id: str, employee_id: str, changes: list[dict], source: str,
                         actor_id: str | None, ref_id: str | None = None) -> None:
    """Append field-level history rows (values already masked and readable). Call inside the changing transaction."""
    for c in changes:
        execute("""INSERT INTO employee_field_history (company_id, employee_id, field, old_value, new_value, source, ref_id, changed_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                [company_id, employee_id, c['label'], None if c.get('from') is None else c['from'][:500],
                 None if c.get('to') is None else c['to'][:500], source, ref_id, actor_id])


# Plain employee columns edited on the HR employee form, with the label used in history.
EMPLOYEE_FIELDS: list[tuple[str, str]] = [
    ('emp_code', 'Employee code'), ('name_en', 'Name'), ('gender', 'Gender'), ('dob', 'Date of birth'), ('marital_status', 'Marital status'),
    ('join_date', 'Join date'), ('exit_date', 'Exit date'), ('designation', 'Designation'), ('grade', 'Grade'), ('phone', 'Phone'), ('email', 'Work email'),
    ('ot_eligible', 'Overtime eligible'), ('work_from_home', 'Work from home'), ('regime', 'Retirement scheme'), ('ssf_id', 'SSF ID'), ('pf_id', 'PF number'),
    ('cit_id', 'CIT ID'), ('bank_code', 'Bank'), ('bank_branch', 'Bank branch'), ('bank_account_last4', 'Bank account'), ('is_resident', 'Resident'),
    ('remote_area_class', 'Remote area class'), ('disability', 'Disability'), ('festival_month', 'Festival month'), ('probation_end', 'Probation end'),
    ('has_pan', 'PAN'), ('branch_id', 'Branch'), ('org_unit_id', 'Department'), ('manager_id', 'Manager'), ('employment_type', 'Employment type'), ('status', 'Status'),
]


def show(v: Any) -> str | None:
    """String(v) as JavaScript writes it: dates as YYYY-MM-DD, booleans as true/false, arrays joined with ', '."""
    if v is None or v == '':
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (list, tuple)):
        return ', '.join(show(x) or '' for x in v)
    if isinstance(v, UUID):
        return str(v)
    return str(v)


def _name(table: str, id_: Any) -> str | None:
    if not id_:
        return None
    r = one(f'SELECT name_en AS n FROM {table} WHERE id = %s', [str(id_)])
    return r['n'] if r else str(id_)


def diff_employee_rows(before: dict, after: dict) -> list[dict]:
    """Compare two employee rows (as the employees module returns them) and return readable changes."""
    out: list[dict] = []
    for col, label in EMPLOYEE_FIELDS:
        b, a = before.get(col), after.get(col)
        if show(b) == show(a):
            continue
        frm, to = show(b), show(a)
        if col == 'branch_id':
            frm, to = _name('branch', b), _name('branch', a)
        if col == 'org_unit_id':
            frm, to = _name('org_unit', b), _name('org_unit', a)
        if col == 'manager_id':
            frm, to = _name('employee', b), _name('employee', a)
        if col == 'bank_account_last4':
            frm, to = (f'•••• {frm}' if frm else None), (f'•••• {to}' if to else None)
        if col == 'has_pan':
            frm, to = ('Recorded' if b else None), ('Recorded' if a else None)
        out.append(change(col, label, frm, to))
    return out
