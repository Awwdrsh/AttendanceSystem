from hajiri.web.nav import NavItem

NAV: list[NavItem] = [
    NavItem(hub='employees', label='Directory', url_name='employees:list', order=10, roles=('hr',), icon='users',
            palette_label='Employees', keywords=('staff', 'add employee', 'import', 'csv', 'pan')),
]
