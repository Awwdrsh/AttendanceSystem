"""
Public interface of the people app (people and core HR). Other slices import from here:

    from hajiri.people.services import (record_career_event, log_career_event, apply_due_career_events, record_field_changes,
                                        diff_employee_rows, read_profile, people_policy, CAREER_LABEL, SECTION_LABEL)

- record_career_event(*, company_id, employee_id, kind, effective_date, details=None, note=None, actor_id=None) -> event row
  (applies it when due; call inside a transaction). log_career_event(...) records an event already in effect.
- record_field_changes(*, company_id, employee_id, changes=[{field, label, from, to}], source, actor_id, ref_id=None).
- read_profile(employee_id, reveal=False) -> {section: {...}} (encrypted numbers masked unless reveal; the caller audits).
- people_policy(company_id) -> {'makerChecker': bool}.
Route-level functions (directory, card, structure, change requests, career...) are in hajiri.people.service.
"""
from .career import (CAREER_KINDS, CAREER_LABEL, TEMPORARY_KINDS, apply_career_event, apply_due_career_events,  # noqa: F401
                     cancel_career_event, log_career_event, record_career_event, split_assignment)
from .change_requests import PROFILE_CHANGE, create_change_request  # noqa: F401
from .history import diff_employee_rows, record_field_changes  # noqa: F401
from .profile import (SECRET_FIELDS, SECTION_LABEL, SECTIONS, SENSITIVE_SECTIONS, apply_section, diff_section, mask,  # noqa: F401
                      parse_section, read_profile)
from .service import people_policy  # noqa: F401
