from hajiri.web.nav import NavItem

# Claims & travel (hubs.tsx MyClaimsHub, /me/claims) and Expenses & travel (ExpensesHub, /expenses, managers and HR).
NAV: list[NavItem] = [
    NavItem(hub='claims', label='Expense claims', url_name='expenses:my_claims', order=10, feature='expenses', employee_only=True,
            icon='receipt-refund', palette_label='My expense claims', keywords=('expense', 'reimbursement', 'receipt', 'claim')),
    NavItem(hub='claims', label='Travel', url_name='expenses:my_travel', order=20, feature='expenses', employee_only=True,
            icon='plane', palette_label='My travel', keywords=('travel', 'tada', 'advance', 'trip', 'settlement')),
    NavItem(hub='expenses', label='Claims', url_name='expenses:claims', order=10, roles=('manager', 'hr'), feature='expenses',
            icon='receipt-refund', palette_label='Expense claims to review', keywords=('claims', 'reimbursement')),
    NavItem(hub='expenses', label='Travel', url_name='expenses:travel', order=20, roles=('manager', 'hr'), feature='expenses',
            icon='plane', palette_label='Travel requests and settlements', keywords=('travel', 'tada', 'advance')),
    NavItem(hub='expenses', label='Policies', url_name='expenses:policies', order=30, roles=('hr',), feature='expenses',
            icon='adjustments-horizontal', palette_label='Expense policies', keywords=('limits', 'mileage', 'tada rates', 'spend')),
]
