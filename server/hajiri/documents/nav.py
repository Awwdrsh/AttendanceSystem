from hajiri.web.nav import NavItem

# ProfileHub 'Documents & letters' (shown to users linked to an employee) and EmployeesHub 'Documents & letters' (HR);
# hubs.tsx positions 2 and 5.
NAV: list[NavItem] = [
    NavItem(hub='profile', label='Documents & letters', url_name='documents:mine', order=20, feature='documents',
            employee_only=True, icon='file-certificate', palette_label='My documents',
            keywords=('letters', 'certificate', 'policy', 'salary certificate')),
    NavItem(hub='employees', label='Documents & letters', url_name='documents:admin', order=50, roles=('hr',),
            feature='documents', icon='file-certificate', keywords=('letters', 'templates', 'policies', 'verify')),
]
