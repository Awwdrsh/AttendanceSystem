from hajiri.web.nav import NavItem, area

NAV: list[NavItem] = [
    NavItem(hub='timesheet', label='Timesheet', url_name='timesheets:mine', order=10, feature='time', employee_only=True,
            icon='hourglass', keywords=('timer', 'project', 'hours', 'log time')),
    NavItem(hub='attendance', label='Timesheets', url_name='timesheets:review', order=50, feature='time',
            roles=('manager', *area('attendance')), icon='hourglass', keywords=('approve timesheets', 'hours report')),
    NavItem(hub='attendance', label='Projects', url_name='timesheets:projects', order=80, feature='time',
            module='attendance', icon='briefcase', keywords=('clients', 'activities', 'time tracking')),
]
