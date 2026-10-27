from hajiri.web.nav import NavItem

NAV: list[NavItem] = [
    NavItem(hub='settings', label='Devices', url_name='devices:list', order=40, roles=('hr',), icon='fingerprint',
            keywords=('terminals', 'biometric', 'pin')),
]
