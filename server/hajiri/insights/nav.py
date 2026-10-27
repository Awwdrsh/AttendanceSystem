from hajiri.web.nav import NavItem

NAV: list[NavItem] = [
    NavItem(hub='insights', label='Insights', url_name='insights:insights', order=10, module='insights', icon='chart-histogram',
            keywords=('analytics', 'headcount', 'attrition', 'people cost', 'trends')),
    NavItem(hub='reports', label='Catalogue', url_name='insights:reports', order=10, roles=('hr',), icon='report-analytics',
            palette_label='Reports', keywords=('export', 'csv', 'register', 'birthdays', 'nominees', 'headcount')),
    NavItem(hub='reports', label='Build a report', url_name='insights:builder', order=20, roles=('hr',), icon='report-analytics',
            keywords=('report builder', 'ad-hoc', 'custom report')),
]
