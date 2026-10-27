from hajiri.web.nav import NavItem

# Tasks is a single-page hub (Layout.tsx: /tasks, feature 'tasks').
NAV: list[NavItem] = [
    NavItem(hub='tasks', label='Tasks', url_name='tasks:board', order=10, feature='tasks', icon='layout-kanban',
            keywords=('to-do', 'todo', 'board', 'kanban', 'checklist')),
]
