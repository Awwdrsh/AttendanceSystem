from hajiri.web.nav import NavItem

NAV: list[NavItem] = [
    NavItem(hub='overview', label='Overview', url_name='dashboard:overview', order=10, module='insights',
            icon='layout-dashboard', keywords=('dashboard',)),
]
