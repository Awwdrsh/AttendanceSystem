from hajiri.web.nav import NavItem, area

NAV: list[NavItem] = [
    NavItem(hub='attendance', label='Work from home', url_name='punch:from_home', order=20, roles=('manager', *area('attendance')),
            icon='home', palette_label='Work from home check-ins', keywords=('remote', 'photo', 'wfh')),
]
