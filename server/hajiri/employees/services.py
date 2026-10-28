"""
Public interface of the employees app (the employee master). Other slices import from here:

    from hajiri.employees.services import list_employees, get_employee, create_employee, update_employee, READ_ALL

- list_employees(user, {status?, search?, branchId?, pan?}) -> rows (HR and the read-all roles company-wide, managers
  their people, everyone themselves); employees_queryset(user, query) the same as a queryset (values rows);
  public_rows(qs) the PUBLIC_COLS shape for any Employee queryset; get_employee(user, id); sensitive(user, id) (HR,
  audited); create_employee(user, body) -> id; update_employee(user, id, body) -> row; import_csv(user, text,
  dry_run=) -> (status, body); assignments(user, id); audit_events(user, id); parse_csv(text) (csv module).
  All scoped to the user's ACTIVE company.
"""
from .service import (PUBLIC_FIELDS, READ_ALL, assignments, audit_events, create_employee, employees_queryset,  # noqa: F401
                      get_employee, import_csv, link_designation, list_employees, parse_csv, public_rows, sensitive,
                      update_employee)
