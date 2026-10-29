"""
Role labels and help texts (group administration, user forms) and the company feature switches as the JSON API reads
them (hajiri.core.api.FeatureEnabled). The menus themselves are the React app's (frontend/src/components/Layout.jsx);
NavItem is kept so the apps' nav.py modules, which describe their pages, still import.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Callable

from hajiri.core.auth import MODULE_ROLES, AuthUser

STAFF_ROLES = ('manager', 'hr', 'payroll_admin', 'attendance_admin', 'recruiter', 'executive')
ROLE_LABEL = {
    'admin': 'Admin', 'hr': 'HR', 'manager': 'Manager', 'employee': 'Employee', 'payroll_admin': 'Payroll admin',
    'attendance_admin': 'Attendance admin', 'recruiter': 'Recruiter', 'executive': 'Executive',
    'group_admin': 'Group admin', 'group_hr': 'Group HR', 'group_executive': 'Group executive',
}
GROUP_ROLE_HELP = {
    'group_admin': 'Admin in every company of the group; adds companies and gives group roles',
    'group_hr': 'HR in every company of the group',
    'group_executive': 'Read-only overview, insights and dashboards of every company of the group',
}
ROLE_HELP = {
    'admin': 'Everything, including users, roles and statutory rules',
    'hr': 'All HR work across the company',
    'manager': 'Their team: approvals, attendance and leave (department and branch heads get this automatically)',
    'employee': 'Self-service: attendance, leave, payslips, claims',
    'payroll_admin': 'Payroll, salary structures, loans and tax only',
    'attendance_admin': 'Attendance, leave, shifts, rosters, field work and time tracking only',
    'recruiter': 'Hiring: jobs, candidates, interviews and offers (not adding employees)',
    'executive': 'Read-only overview, insights and dashboards',
}


def area(name: str) -> tuple[str, ...]:
    """Roles of an HR area (lib/access.ts AREA), e.g. area('attendance') == ('hr', 'attendance_admin')."""
    return MODULE_ROLES[name]


@dataclass(frozen=True)
class NavItem:
    hub: str                       # hub key from HUBS below, e.g. 'attendance', 'my_attendance', 'settings'
    label: str                     # tab label, English
    url_name: str                  # Django URL name of the tab page (no arguments), e.g. 'shifts:list'
    order: int = 100               # position inside the hub; the lowest visible one is the hub's landing tab
    roles: tuple[str, ...] = ()    # any of these roles may see it (admin always may); empty = every signed-in user
    module: str | None = None      # MODULE_ROLES area ('payroll', 'attendance', 'hiring', 'insights') instead of roles
    feature: str | None = None     # company feature switch that must be on (Settings -> Features), if any
    icon: str | None = None        # Tabler icon name without the 'ti-' prefix (palette entry)
    keywords: tuple[str, ...] = field(default=())  # extra words for the command palette
    employee_only: bool = False    # only for accounts linked to an employee (hubs.tsx `show: !!user.employeeId`)
    palette_label: str | None = None  # label in the command palette when it should differ from the tab label
    # Extra visibility rule, e.g. group roles (hajiri.core.companies.can_open_group): shown only when it is true.
    visible: Callable[[AuthUser], bool] | None = None


# ---------------------------------------------------------------- feature switches

FEATURE_KEYS = ('time', 'field', 'expenses', 'performance', 'learning', 'hiring', 'lifecycle', 'documents', 'helpdesk',
                'tasks', 'kudos', 'surveys', 'suggestions')


def disabled_features(company_id: str | None) -> set[str]:
    """
    The company's switched-off modules. Uses the features app when it provides `disabled_features`; otherwise reads
    the same company_setting row. If they cannot be read, everything counts as on (the server refuses switched-off
    modules anyway), as in lib/features.tsx.
    """
    if not company_id:
        return set()
    try:
        svc = import_module('hajiri.features.services')
        fn = getattr(svc, 'disabled_features', None)
        if fn:
            return set(fn(company_id))
    except ModuleNotFoundError:
        pass
    from hajiri.core.settings_store import get_setting
    val = get_setting(company_id, 'features', {}) or {}
    raw = val.get('off') if isinstance(val, dict) else None
    return {k for k in raw if isinstance(k, str) and k in FEATURE_KEYS} if isinstance(raw, list) else set()


def feature_on(request, key: str) -> bool:
    """For views and templates: is this feature switched on for the signed-in user's company?"""
    user = getattr(request, 'hajiri_user', None)
    off = getattr(request, '_hj_features_off', None)
    if off is None:
        off = disabled_features(user.company_id if user else None)
        request._hj_features_off = off
    return key not in off
