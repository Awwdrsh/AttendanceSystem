from hajiri.web.nav import NavItem

NAV: list[NavItem] = [
    NavItem(hub='people', label='Directory', url_name='people:directory', order=10, icon='address-book',
            palette_label='People directory', keywords=('colleagues', 'phone', 'email', 'find someone')),
    NavItem(hub='people', label='Org chart', url_name='people:org_chart', order=20, icon='sitemap', keywords=('reporting', 'manager', 'team')),
    NavItem(hub='employees', label='Org structure', url_name='people:structure', order=20, roles=('hr',), icon='building',
            keywords=('departments', 'teams', 'branches', 'designations')),
    NavItem(hub='employees', label='Change requests', url_name='people:requests', order=60, roles=('hr',), icon='user-edit',
            palette_label='Profile change requests', keywords=('profile', 'bank', 'approve')),
    NavItem(hub='profile', label='Profile', url_name='people:profile', order=10, icon='user-circle', palette_label='My profile',
            keywords=('photo', 'address', 'emergency contact', 'bank account', 'family', 'nominee')),
    NavItem(hub='profile', label='Privacy & my data', url_name='people:privacy', order=50, employee_only=True, icon='shield-lock',
            palette_label='Privacy and my data', keywords=('download my data', 'consent', 'biometric', 'clock-in log')),
]
