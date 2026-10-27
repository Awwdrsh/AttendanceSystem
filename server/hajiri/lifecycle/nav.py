from hajiri.web.nav import NavItem

NAV: list[NavItem] = [
    NavItem(hub='employees', label='Onboarding', url_name='lifecycle:onboarding', order=30, roles=('hr',), feature='lifecycle',
            icon='user-plus', keywords=('joiners', 'probation', 'checklist', 'assets', 'laptop')),
    NavItem(hub='employees', label='Exits', url_name='lifecycle:exits', order=40, roles=('hr',), feature='lifecycle', icon='logout',
            keywords=('resignation', 'clearance', 'exit interview', 'final settlement')),
]
