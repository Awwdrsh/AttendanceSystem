from hajiri.core.companies import can_open_group
from hajiri.web.nav import NavItem


# The Group hub (hajiri.web.nav.HUBS 'group'). The overview is visible to people with a group role, people who may
# see figures of several companies, and admins (who can turn their company into a group by adding a sister company).
# The directory and reports are for people who may use group views (services.can_view).


def can_view(user) -> bool:
    from .services import can_view as rule      # imported late: services pulls in the attendance and devices apps
    return rule(user)


NAV: list[NavItem] = [
    NavItem(hub='group', label='Overview', url_name='group:overview', order=10, icon='building-community',
            visible=can_open_group, palette_label='Group overview',
            keywords=('companies', 'group', 'sister company', 'switch company', 'holding')),
    NavItem(hub='group', label='People', url_name='group:people', order=12, icon='users-group', visible=can_view,
            palette_label='Group directory and headcount',
            keywords=('directory', 'headcount', 'all companies', 'employees across companies')),
    NavItem(hub='group', label='Reports', url_name='group:reports', order=14, icon='report-analytics',
            visible=can_view, palette_label='Group reports',
            keywords=('consolidated', 'payroll summary', 'attrition', 'attendance summary', 'overtime', 'leave')),
]
