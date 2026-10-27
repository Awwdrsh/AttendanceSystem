from hajiri.web.nav import NavItem

# hubs.tsx GrowthHub and PerformanceHub (the Learning tabs are in hajiri/learning/nav.py).
NAV: list[NavItem] = [
    NavItem(hub='growth', label='Goals', url_name='performance:my_goals', order=10, feature='performance', icon='target-arrow',
            palette_label='My goals', keywords=('kpi', 'okr', 'key results')),
    NavItem(hub='growth', label='Reviews', url_name='performance:my_reviews', order=20, feature='performance', icon='clipboard-check',
            palette_label='My reviews', keywords=('appraisal', 'self review', 'peer feedback')),
    NavItem(hub='growth', label='1-on-1s', url_name='performance:one_on_ones', order=30, feature='performance', icon='messages',
            keywords=('one on one', 'meeting', 'agenda')),
    NavItem(hub='growth', label='Feedback', url_name='performance:feedback', order=40, feature='performance', icon='message-circle',
            keywords=('praise', 'request feedback')),
    NavItem(hub='performance', label='Goals', url_name='performance:team_goals', order=10, feature='performance', roles=('manager', 'hr'),
            icon='target-arrow', palette_label='Team goals', keywords=('kpi library', 'journal', 'company goals')),
    NavItem(hub='performance', label='Review cycles', url_name='performance:cycles', order=20, feature='performance', roles=('manager', 'hr'),
            icon='clipboard-check', keywords=('appraisal', 'calibration', 'competencies')),
]
