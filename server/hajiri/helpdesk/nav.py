from hajiri.web.nav import NavItem

# HelpHub (hubs.tsx): /me/help 'Questions' (the 'Grievances' tab belongs to the relations app); HelpdeskPage for HR.
NAV: list[NavItem] = [
    NavItem(hub='help', label='Questions', url_name='helpdesk:mine', order=10, feature='helpdesk', icon='lifebuoy',
            palette_label='Help desk', keywords=('ticket', 'question', 'hr help', 'resign')),
    NavItem(hub='helpdesk', label='Help desk', url_name='helpdesk:queue', order=10, roles=('hr',), feature='helpdesk',
            icon='lifebuoy', keywords=('tickets', 'queries')),
]
