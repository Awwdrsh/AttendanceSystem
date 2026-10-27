from hajiri.web.nav import NavItem

NAV: list[NavItem] = [
    NavItem(hub='growth', label='Learning', url_name='learning:mine', order=50, feature='learning', icon='school',
            palette_label='My learning', keywords=('training', 'course', 'certificate', 'assessment')),
    NavItem(hub='performance', label='Learning', url_name='learning:admin', order=30, feature='learning', roles=('manager', 'hr'),
            icon='school', palette_label='Training and courses', keywords=('training', 'course', 'sessions', 'certificates', 'training spend')),
]
