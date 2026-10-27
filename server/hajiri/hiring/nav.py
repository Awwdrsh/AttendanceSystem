from hajiri.web.nav import NavItem

_STAFF = ('manager', 'hr', 'recruiter')

NAV: list[NavItem] = [
    NavItem(hub='hiring', label='Jobs', url_name='hiring:jobs', order=10, roles=_STAFF, feature='hiring', icon='briefcase',
            palette_label='Hiring: jobs', keywords=('vacancy', 'careers page', 'recruitment')),
    NavItem(hub='hiring', label='Candidates', url_name='hiring:pipeline', order=20, roles=_STAFF, feature='hiring', icon='users',
            palette_label='Hiring: candidates', keywords=('pipeline', 'applicants')),
    NavItem(hub='hiring', label='Interviews', url_name='hiring:interviews', order=30, roles=_STAFF, feature='hiring',
            icon='calendar-event', palette_label='Hiring: interviews', keywords=('scorecard', 'panel')),
    NavItem(hub='hiring', label='Requisitions', url_name='hiring:requisitions', order=40, roles=_STAFF, feature='hiring',
            icon='user-plus', palette_label='Hiring: requisitions', keywords=('hiring request', 'headcount')),
]
