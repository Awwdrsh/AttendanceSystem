from hajiri.web.nav import NavItem


def _approvals_badge(user) -> int:
    from .service import inbox_count
    return inbox_count(user)


NAV: list[NavItem] = [
    # The Approvals hub shows for staff roles, and for anyone with something to decide (nav.build uses the badge).
    NavItem(hub='approvals', label='Approvals', url_name='workflow:approvals', order=10, icon='list-check',
            keywords=('approve', 'requests', 'inbox')),
    NavItem(hub='leave', label='Request history', url_name='workflow:history', order=40, module='attendance', icon='history',
            keywords=('requests', 'leave requests', 'corrections', 'overtime')),
    NavItem(hub='settings', label='Approval flows', url_name='workflow:flows', order=70, roles=('hr',), icon='list-check',
            keywords=('workflow', 'approvers', 'delegation', 'user groups')),
]

BADGES = {'approvals': _approvals_badge}
