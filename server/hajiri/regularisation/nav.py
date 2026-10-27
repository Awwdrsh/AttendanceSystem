from hajiri.web.nav import NavItem, area

NAV: list[NavItem] = [
    NavItem(hub='attendance', label='Fix a punch', url_name='regularisation:fix', order=130,
            roles=('manager', *area('attendance')), icon='clock-edit', keywords=('fix punch', 'missed punch', 'correction')),
]
