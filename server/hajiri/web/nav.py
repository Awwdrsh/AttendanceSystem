"""
Navigation contract. Every app declares its menu entries in its own `nav.py` as `NAV: list[NavItem]`; the layout
collects them, so no app edits a shared menu file.

The sidebar lists HUBS (the entries of apps/web/src/components/Layout.tsx). Each NavItem is one tab of a hub
(apps/web/src/pages/hubs.tsx): a hub with one visible tab is a plain page, with more it shows a tab strip. A hub is
listed in the sidebar while at least one of its tabs is visible to the signed-in user. Every visible tab is also a
command palette entry.

An app's nav.py may also define `BADGES: dict[str, Callable[[AuthUser], int]]` to feed a counter shown on a hub
('approvals' for the Approvals entry, 'notifications' for the bell).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import import_module
from typing import Callable

from django.apps import apps
from django.core.signals import setting_changed
from django.urls import NoReverseMatch, Resolver404, resolve, reverse

from hajiri.core.auth import MODULE_ROLES, AuthUser, has_role

log = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class Hub:
    key: str
    title: str
    group: str                     # 'me' | 'company' | 'manage' | 'settings' | 'account'
    path: str                      # the sidebar address; the hub is "current" on this path and below it
    icon: str
    roles: tuple[str, ...] = ()    # who sees the sidebar entry (any of; admin always); empty = everyone
    employee_only: bool = False
    features: tuple[str, ...] = ()  # shown while any of these feature switches is on
    keywords: tuple[str, ...] = ()
    badge: str | None = None       # BADGES key whose count is shown next to the entry


GROUP_TITLES = {'me': 'My work', 'company': 'Company', 'manage': 'Manage'}

# Sidebar order mirrors Layout.tsx NAV; tab sets mirror hubs.tsx.
HUBS: tuple[Hub, ...] = (
    Hub('home', 'Home', 'me', '/', 'home', employee_only=True, keywords=('today', 'clock in', 'clock out', 'punch')),
    Hub('my_attendance', 'My attendance', 'me', '/me/attendance', 'calendar-stats', employee_only=True, keywords=('field visit', 'roster', 'shift swap')),
    Hub('time_off', 'Time off', 'me', '/me/leave', 'beach', employee_only=True, keywords=('leave', 'holiday', 'vacation', 'apply')),
    Hub('timesheet', 'Timesheet', 'me', '/me/timesheet', 'hourglass', employee_only=True, features=('time',), keywords=('timer', 'project', 'hours', 'log time')),
    Hub('payslips', 'Payslips', 'me', '/me/payslips', 'receipt-2', employee_only=True, keywords=('salary', 'pay', 'tax')),
    Hub('claims', 'Claims & travel', 'me', '/me/claims', 'receipt-refund', employee_only=True, features=('expenses',), keywords=('expense', 'reimbursement', 'travel', 'tada', 'advance')),
    Hub('growth', 'Growth', 'me', '/me/growth', 'target-arrow', employee_only=True, features=('performance', 'learning'), keywords=('goals', 'kpi', 'review', 'appraisal', 'training', 'learning', '1-on-1')),
    Hub('tasks', 'Tasks', 'me', '/tasks', 'layout-kanban', features=('tasks',), keywords=('to-do', 'todo', 'board', 'kanban', 'checklist')),
    Hub('help', 'Help desk', 'me', '/me/help', 'lifebuoy', employee_only=True, features=('helpdesk',), keywords=('ticket', 'question', 'hr help', 'resign', 'grievance', 'complaint')),
    Hub('people', 'People', 'company', '/people', 'address-book', keywords=('directory', 'org chart', 'colleagues')),
    Hub('engage', 'Engage', 'company', '/engage', 'confetti', keywords=('kudos', 'announcements', 'birthdays', 'surveys', 'polls')),
    # Groups of companies: the group overview and group administration (hajiri.group, hajiri.group_admin); its tabs
    # decide who sees it (group roles, or figures of several companies).
    Hub('group', 'Group', 'manage', '/group', 'building-community', keywords=('companies', 'sister company', 'group overview', 'switch company')),
    Hub('overview', 'Overview', 'manage', '/overview', 'layout-dashboard', roles=('hr', 'executive'), keywords=('dashboard',)),
    Hub('approvals', 'Approvals', 'manage', '/approvals', 'list-check', roles=STAFF_ROLES, badge='approvals', keywords=('approve', 'requests', 'inbox')),
    Hub('employees', 'Employees', 'manage', '/employees', 'users', roles=('hr',), keywords=('staff', 'people', 'add employee', 'onboarding', 'exit', 'documents', 'letters')),
    Hub('attendance', 'Attendance', 'manage', '/attendance', 'clock-hour-4', roles=('manager', *MODULE_ROLES['attendance']), keywords=("who's in", 'today', 'exceptions', 'overtime', 'fix punch', 'timesheets', 'roster', 'kiosk')),
    Hub('leave', 'Leave', 'manage', '/leave', 'calendar-event', roles=('manager', *MODULE_ROLES['attendance']), keywords=('calendar', 'balances', 'policies', 'time off')),
    Hub('payroll', 'Payroll', 'manage', '/payroll', 'cash', roles=MODULE_ROLES['payroll'], keywords=('pay run', 'salary', 'tax', 'loans', 'ssf')),
    Hub('expenses', 'Expenses & travel', 'manage', '/expenses', 'receipt-refund', roles=('manager', 'hr'), features=('expenses',), keywords=('claims', 'reimbursement', 'travel', 'tada')),
    Hub('performance', 'Performance', 'manage', '/performance', 'target-arrow', roles=('manager', 'hr'), features=('performance', 'learning'), keywords=('goals', 'kpi', 'appraisal', 'review cycle', 'training', 'learning')),
    Hub('hiring', 'Hiring', 'manage', '/hiring', 'user-plus', roles=('manager', *MODULE_ROLES['hiring']), features=('hiring',), keywords=('jobs', 'vacancy', 'candidates', 'interview', 'offer', 'recruitment')),
    Hub('helpdesk', 'Help desk', 'manage', '/helpdesk', 'lifebuoy', roles=('hr',), features=('helpdesk',), keywords=('tickets', 'queries')),
    Hub('relations', 'Employee relations', 'manage', '/relations', 'scale', roles=('hr',), keywords=('grievance', 'disciplinary', 'warning', 'milestones', 'retirement', 'contract end', 'acting', 'deputation', 'kye', 'devices')),
    Hub('insights', 'Insights', 'manage', '/insights', 'chart-histogram', roles=MODULE_ROLES['insights'], keywords=('analytics', 'headcount', 'attrition', 'people cost', 'trends')),
    Hub('reports', 'Reports', 'manage', '/reports', 'report-analytics', roles=('hr',), keywords=('export', 'csv', 'register', 'report builder', 'birthdays', 'nominees', 'headcount')),
    Hub('settings', 'Settings', 'settings', '/settings', 'settings', roles=('hr',), keywords=('shifts', 'holidays', 'devices', 'users', 'rules', 'setup')),
    # Account menu (bottom of the sidebar), not sidebar entries.
    Hub('notifications', 'Notifications', 'account', '/me/notifications', 'bell', badge='notifications'),
    Hub('profile', 'Profile', 'account', '/me/profile', 'user-circle'),
)
HUB_BY_KEY = {h.key: h for h in HUBS}

# Account menu entries (Layout.tsx ACCOUNT). Paths are fixed so the menu works before the owning slice registers.
ACCOUNT_LINKS: tuple[tuple[str, str, str, str | None], ...] = (
    ('/me/notifications', 'Notifications', 'bell', None),
    ('/me/profile', 'Profile & privacy', 'user-circle', None),
    ('/me/profile/documents', 'My documents', 'file-certificate', 'documents'),
    ('/me/profile/security', 'Sign-in security', 'user-shield', None),
)

# web's own tab: Profile -> Sign-in security.
NAV: list[NavItem] = [
    NavItem(hub='profile', label='Sign-in security', url_name='web:security', order=30, icon='user-shield',
            keywords=('two-step', '2fa', 'authenticator', 'sessions', 'sign out', 'password')),
]


def _nav_modules():
    for cfg in apps.get_app_configs():
        if not cfg.name.startswith('hajiri.'):
            continue
        try:
            mod = import_module(f'{cfg.name}.nav')
        except ModuleNotFoundError as e:
            if e.name != f'{cfg.name}.nav':
                raise
            continue
        yield mod


def all_items() -> list[NavItem]:
    items: list[NavItem] = []
    for mod in _nav_modules():
        for it in getattr(mod, 'NAV', []):
            if it.hub not in HUB_BY_KEY:
                log.warning('nav item %r names unknown hub %r', it.label, it.hub)
                continue
            items.append(it)
    return sorted(items, key=lambda i: (i.hub, i.order, i.label))


def all_badges() -> dict[str, Callable[[AuthUser], int]]:
    out: dict[str, Callable[[AuthUser], int]] = {}
    for mod in _nav_modules():
        out.update(getattr(mod, 'BADGES', {}) or {})
    return out


@lru_cache(maxsize=512)
def url_of(url_name: str) -> str | None:
    try:
        return reverse(url_name)
    except NoReverseMatch:
        return None


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


# ---------------------------------------------------------------- visibility

@dataclass
class Tab:
    item: NavItem
    url: str
    label: str
    active: bool = False


@dataclass
class HubView:
    hub: Hub
    tabs: list[Tab]
    url: str
    active: bool = False
    badge: int = 0

    @property
    def title(self) -> str:
        return self.hub.title


def item_visible(user: AuthUser, it: NavItem, off: set[str]) -> bool:
    if it.employee_only and not user.employee_id:
        return False
    if it.feature and it.feature in off:
        return False
    if it.module and not has_role(user, *MODULE_ROLES[it.module]):
        return False
    if it.roles and not has_role(user, *it.roles):
        return False
    if it.visible is not None and not it.visible(user):
        return False
    return True


def _path_match(path: str, base: str) -> bool:
    if base == '/':
        return path == '/'
    return path == base or path.startswith(base.rstrip('/') + '/')


def _norm(path: str) -> str:
    return path.rstrip('/') or '/'


@lru_cache(maxsize=64)
def _resolves(path: str) -> bool:
    try:
        resolve(path)
        return True
    except Resolver404:
        return False


@dataclass
class Navigation:
    groups: list[tuple[str, list[HubView]]]
    settings: HubView | None
    account: list[dict]
    current: HubView | None          # the hub of the current page (sidebar highlight)
    current_tab: Tab | None          # the tab of the current page, when the page is a hub tab
    palette: list[dict]
    unread: int = 0

    @property
    def hub_tabs(self) -> list[Tab]:
        """Tabs to draw above the page: only on a tab page and only when the hub has more than one."""
        if not self.current_tab or not self.current:
            return []
        return self.current.tabs if len(self.current.tabs) > 1 else []

    @property
    def embedded(self) -> bool:
        """The page is a hub tab: the hub header shows the title, so page_header drops its own."""
        return self.current_tab is not None


def hub_landing(user: AuthUser, hub_key: str, off: set[str] | None = None) -> str | None:
    """URL of the first tab of a hub the user may see (Hub.tsx redirects there when the asked-for tab is hidden)."""
    off = disabled_features(user.company_id) if off is None else off
    for it in all_items():
        if it.hub == hub_key and item_visible(user, it, off):
            url = url_of(it.url_name)
            if url:
                return url
    return None


def build(user: AuthUser, path: str, *, items: list[NavItem] | None = None, off: set[str] | None = None,
          badges: dict[str, int] | None = None) -> Navigation:
    items = all_items() if items is None else items
    off = disabled_features(user.company_id) if off is None else off
    if badges is None:
        badges = {}
        for key, fn in all_badges().items():
            try:
                badges[key] = int(fn(user) or 0)
            except Exception:  # noqa: BLE001 - a broken counter must not break every page
                log.exception('badge %s failed', key)
    cur = _norm(path)
    by_hub: dict[str, list[Tab]] = {}
    for it in items:
        if not item_visible(user, it, off):
            continue
        url = url_of(it.url_name)
        if url is None:
            continue
        by_hub.setdefault(it.hub, []).append(Tab(it, url, it.label, _norm(url) == cur))

    views: dict[str, HubView] = {}
    for h in HUBS:
        tabs = by_hub.get(h.key, [])
        if not tabs:
            continue
        count = badges.get(h.badge, 0) if h.badge else 0
        allowed = (not h.roles or has_role(user, *h.roles)) or (h.badge == 'approvals' and count > 0)
        if not allowed or (h.employee_only and not user.employee_id):
            continue
        if h.features and all(f in off for f in h.features):
            continue
        views[h.key] = HubView(h, tabs, tabs[0].url, badge=count)

    # The current hub: the page is one of its tabs, or sits under its sidebar path (longest path wins).
    current_tab = next((t for v in views.values() for t in v.tabs if t.active), None)
    current = next((v for v in views.values() if current_tab and current_tab in v.tabs), None)
    if current is None:
        cands = [v for v in views.values() if _path_match(cur, v.hub.path)]
        current = max(cands, key=lambda v: len(v.hub.path), default=None)
    if current:
        current.active = True

    manage_ok = has_role(user, *STAFF_ROLES) or badges.get('approvals', 0) > 0
    groups = []
    for g in ('me', 'company', 'manage'):
        if g == 'manage' and not manage_ok:
            continue
        hubs = [views[h.key] for h in HUBS if h.group == g and h.key in views]
        if hubs:
            groups.append((GROUP_TITLES[g], hubs))
    settings = views.get('settings')

    account = [{'url': u, 'label': label, 'icon': icon, 'active': _norm(u) == cur}
               for (u, label, icon, feat) in ACCOUNT_LINKS if not (feat and feat in off) and _resolves(u)]

    palette: list[dict] = []
    seen: set[str] = set()

    def add(url: str, label: str, group: str, icon: str, keywords=()):
        if url in seen:
            return
        seen.add(url)
        palette.append({'url': url, 'label': label, 'group': group, 'icon': icon, 'keywords': ' '.join(keywords)})

    for title, hubs in groups:
        for v in hubs:
            add(v.url, v.hub.title, title, v.hub.icon, v.hub.keywords)
    if settings:
        add(settings.url, settings.hub.title, 'Settings', settings.hub.icon, settings.hub.keywords)
    for v in views.values():
        for t in v.tabs:
            add(t.url, t.item.palette_label or t.label, v.hub.title, t.item.icon or v.hub.icon, t.item.keywords)
    for a in account:
        add(a['url'], a['label'], 'Account', a['icon'])

    return Navigation(groups, settings, account, current, current_tab, palette, badges.get('notifications', 0))


def _clear_url_caches(*, setting, **kwargs) -> None:
    """URL lookups are cached per process; drop them when the URLconf changes (override_settings in tests), as
    Django does for its own resolver cache."""
    if setting == 'ROOT_URLCONF':
        url_of.cache_clear()
        _resolves.cache_clear()


setting_changed.connect(_clear_url_caches, dispatch_uid='hajiri.web.nav.url_caches')
