"""
Public interface of the employees app (the employee master). Other slices import from here:

    from hajiri.employees.services import list_employees, get_employee, create_employee, update_employee, READ_ALL

- list_employees(user, {status?, search?, branchId?, pan?}) -> rows (HR and the read-all roles company-wide, managers
  their people, everyone themselves); get_employee(user, id); sensitive(user, id) (HR, audited);
  create_employee(user, body) -> id; update_employee(user, id, body) -> row; import_csv(user, text, dry_run=) ->
  (status, body); assignments(user, id); audit_events(user, id); parse_csv(text).
"""
from .service import (PUBLIC_COLS, READ_ALL, assignments, audit_events, create_employee, get_employee, import_csv,  # noqa: F401
                      link_designation, list_employees, parse_csv, sensitive, update_employee)
