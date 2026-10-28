"""
The sign-in account behind an employee record. An employee record belongs to one company; the person signs in with
their home account (app_user.employee_id) or, in a sister company of the group, through a company_membership whose
employee_id is that record. Notifications about an employee go to whichever account holds the record.

The lookup lives in hajiri.notifications.service (every slice's notices use it); this module re-exports it for the
people, employees and lifecycle code.
"""
from __future__ import annotations

from hajiri.notifications.service import user_id_for_employee

__all__ = ['user_id_for_employee']
