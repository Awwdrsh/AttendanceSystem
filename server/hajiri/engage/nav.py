from hajiri.web.nav import NavItem

# hubs.tsx EngageHub: Feed, Surveys & polls (feature surveys), Suggestions (HR, feature suggestions).
NAV: list[NavItem] = [
    NavItem(hub='engage', label='Feed', url_name='engage:feed', order=10, icon='confetti',
            keywords=('kudos', 'announcements', 'birthdays', 'celebrations'), palette_label='Engage'),
    NavItem(hub='engage', label='Surveys & polls', url_name='engage:surveys', order=20, feature='surveys',
            icon='clipboard-list', keywords=('survey', 'poll', 'pulse', 'enps')),
    NavItem(hub='engage', label='Suggestions', url_name='engage:suggestions', order=30, roles=('hr',), feature='suggestions',
            icon='bulb', keywords=('suggestion box', 'anonymous', 'ideas')),
]
