from hajiri.web.nav import NavItem

# Attendance hub tab "Overtime" (hubs.tsx AttendanceHub, 4th tab) and the palette jump "Overtime review" (Layout.tsx).
NAV: list[NavItem] = [
    NavItem(hub='attendance', label='Overtime', url_name='ot:overtime', order=40, module='attendance', icon='clock-hour-4',
            palette_label='Overtime review', keywords=('ot slips', 'comp-off', 'overtime register')),
]
