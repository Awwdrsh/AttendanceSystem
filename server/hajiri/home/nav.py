from hajiri.web.nav import NavItem

NAV: list[NavItem] = [
    NavItem(hub='home', label='Home', url_name='home:index', order=10, employee_only=True, icon='home',
            keywords=('today', 'clock in', 'clock out', 'punch')),
    NavItem(hub='notifications', label='Notifications', url_name='home:notifications', order=10, icon='bell'),
    NavItem(hub='settings', label='Company & rules', url_name='home:settings', order=10, roles=('hr',), icon='settings',
            palette_label='Settings'),
    NavItem(hub='settings', label='Users & roles', url_name='home:users', order=60, roles=('hr',), icon='user-shield'),
    NavItem(hub='settings', label='Features', url_name='home:features', order=80, roles=('hr',), icon='settings',
            palette_label='Features (switch modules on or off)'),
    NavItem(hub='settings', label='Setup checklist', url_name='home:setup', order=90, roles=('hr',), icon='checklist'),
]


def _unread(user) -> int:
    from hajiri.notifications.service import unread_count
    return unread_count(user.id)


# The red dot on the bell (Layout.tsx polls GET /notifications?unreadOnly).
BADGES = {'notifications': _unread}
