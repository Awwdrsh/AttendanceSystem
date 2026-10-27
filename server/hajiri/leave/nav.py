from hajiri.web.nav import NavItem

# Time off (Layout.tsx "Time off", /me/leave) and the Leave hub (hubs.tsx LeaveHub). Request history (order 40) is the
# workflow app's tab.
NAV: list[NavItem] = [
    NavItem(hub='time_off', label='Time off', url_name='leave:mine', order=10, employee_only=True, icon='beach',
            keywords=('leave', 'holiday', 'vacation', 'apply')),
    NavItem(hub='leave', label='Calendar', url_name='leave:calendar', order=10, roles=('manager', 'hr', 'attendance_admin'),
            icon='calendar-event', palette_label='Team calendar', keywords=('who is off', 'leave calendar')),
    NavItem(hub='leave', label='Balances', url_name='leave:balances', order=20, module='attendance', icon='adjustments',
            palette_label='Leave balances', keywords=('adjust', 'allocation', 'ledger')),
    NavItem(hub='leave', label='Policies', url_name='leave:policies', order=30, module='attendance', icon='adjustments-horizontal',
            palette_label='Leave policies', keywords=('leave types', 'sandwich', 'accrual')),
]
