from hajiri.web.nav import NavItem

from .common import can_administer, can_transfer

# Tabs of the Group hub (after the Overview tab of hajiri.group).
NAV: list[NavItem] = [
    NavItem(hub='group', label='Companies', url_name='group_admin:companies', order=20, icon='building',
            visible=can_administer, palette_label='Companies of the group',
            keywords=('add company', 'sister company', 'PAN', 'logo', 'attach company')),
    NavItem(hub='group', label='Access', url_name='group_admin:access', order=30, icon='key',
            visible=can_administer, palette_label='Access to the companies of the group',
            keywords=('invite', 'roles per company', 'give access', 'remove access')),
    NavItem(hub='group', label='Transfers', url_name='group_admin:transfers', order=40, icon='arrows-exchange',
            visible=can_transfer, palette_label='Transfer an employee to a sister company',
            keywords=('move employee', 'inter-company transfer', 'sister company')),
    NavItem(hub='group', label='Policies', url_name='group_admin:policies', order=50, icon='copy',
            visible=can_administer, palette_label='Copy policies between companies',
            keywords=('copy leave types', 'copy salary structure', 'copy holidays', 'copy shifts', 'templates')),
]
