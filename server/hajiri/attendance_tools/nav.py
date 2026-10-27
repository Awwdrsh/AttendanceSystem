from hajiri.web.nav import NavItem, area

# Attendance hub tabs (hubs.tsx AttendanceHub; order = position x 10) and My attendance (MyAttendanceHub).
NAV: list[NavItem] = [
    NavItem(hub='attendance', label='Today', url_name='attendance_tools:today', order=10, roles=('manager', *area('attendance')),
            icon='users', palette_label="Who's in today", keywords=("who's in", 'team today', 'late', 'absent')),
    NavItem(hub='attendance', label='Exceptions', url_name='attendance_tools:exceptions', order=30, module='attendance',
            icon='alert-triangle', palette_label='Attendance exceptions', keywords=('missing punch', 'recalculate')),
    NavItem(hub='attendance', label='Issues', url_name='attendance_tools:issues', order=100, roles=('manager', *area('attendance')),
            icon='alert-triangle', palette_label='Attendance issues and reminders', keywords=('remind', 'nudge', 'before payroll')),
    NavItem(hub='attendance', label='Late penalties', url_name='attendance_tools:penalties', order=110, module='attendance',
            icon='clock-hour-4', palette_label='Late penalties and attendance rules', keywords=('late arrival', 'block clock-in', "who's out")),
    NavItem(hub='attendance', label='Upload', url_name='attendance_tools:upload', order=120, module='attendance', icon='clock-edit',
            palette_label='Upload attendance (CSV)', keywords=('csv', 'import punches', 'device offline')),
    NavItem(hub='my_attendance', label='Attendance', url_name='attendance_tools:my_attendance', order=10, employee_only=True,
            icon='calendar-stats', palette_label='My attendance', keywords=('my punches', 'correction', 'late days')),
]
