from hajiri.web.nav import NavItem, area

NAV: list[NavItem] = [
    NavItem(hub='attendance', label='Rosters', url_name='shifts:rosters', order=60, roles=('manager', *area('attendance')),
            icon='calendar-event', palette_label='Rosters & shift swaps', keywords=('roster', 'shift swap', 'copy last week', 'publish')),
    NavItem(hub='settings', label='Shifts', url_name='shifts:list', order=20, roles=('hr',), icon='clock-hour-4',
            keywords=('working hours', 'grace', 'night shift', 'assign shift')),
    NavItem(hub='settings', label='Holidays', url_name='shifts:holidays', order=30, roles=('hr',), icon='calendar-event',
            keywords=('public holidays', 'dashain', 'tihar', 'calendar')),
]
