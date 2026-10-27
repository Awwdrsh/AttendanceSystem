from hajiri.web.nav import NavItem

# hubs.tsx RelationsHub (HR) plus the relations tabs of HelpHub, ProfileHub and MyAttendanceHub; JUMPS in Layout.tsx.
NAV: list[NavItem] = [
    NavItem(hub='relations', label='Grievances', url_name='relations:grievances', order=10, roles=('hr',), icon='scale',
            palette_label='Employee relations', keywords=('grievance', 'committee', 'complaint')),
    NavItem(hub='relations', label='Disciplinary', url_name='relations:disciplinary', order=20, roles=('hr',), icon='scale',
            palette_label='Disciplinary cases', keywords=('warning', 'show cause', 'suspension')),
    NavItem(hub='relations', label='Milestones', url_name='relations:milestones', order=30, roles=('hr',), icon='calendar-event',
            palette_label='Milestones (retirements, contract ends)', keywords=('retirement', 'contract end', 'anniversary')),
    NavItem(hub='relations', label='Acting & deputation', url_name='relations:assignments', order=40, roles=('hr',), icon='switch-horizontal',
            keywords=('acting', 'deputation', 'temporary role')),
    NavItem(hub='relations', label='Know your employee', url_name='relations:kye', order=50, roles=('hr',), icon='checklist',
            keywords=('kye', 'record check', 'completeness')),
    NavItem(hub='relations', label='Clock-in devices', url_name='relations:devices', order=60, roles=('hr',), icon='device-mobile',
            keywords=('registered devices', 'phone')),
    NavItem(hub='help', label='Grievances', url_name='relations:my_grievances', order=20, feature='helpdesk', icon='lifebuoy',
            palette_label='Raise a grievance', keywords=('grievance', 'complaint')),
    NavItem(hub='profile', label='Conduct', url_name='relations:conduct', order=40, employee_only=True, icon='scale',
            keywords=('disciplinary', 'show cause', 'notice')),
    NavItem(hub='my_attendance', label='Clock-in devices', url_name='relations:my_devices', order=50, icon='device-mobile',
            keywords=('register phone', 'device')),
]
