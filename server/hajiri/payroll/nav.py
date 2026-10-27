from hajiri.web.nav import NavItem

# Payroll hub (hubs.tsx PayrollHub, AREA.payroll) and Payslips (MyPayHub).
NAV: list[NavItem] = [
    NavItem(hub='payroll', label='Pay runs', url_name='payroll:runs', order=10, module='payroll', icon='cash',
            palette_label='Pay runs', keywords=('pay run', 'salary sheet', 'payslips', 'bonus', 'final settlement', 'arrears')),
    NavItem(hub='payroll', label='Salary structures', url_name='payroll:structures', order=20, module='payroll', icon='stack-2',
            keywords=('components', 'formula')),
    NavItem(hub='payroll', label='Employee salaries', url_name='payroll:assignments', order=30, module='payroll', icon='user-dollar',
            keywords=('assign salary', 'base amounts', 'revision')),
    NavItem(hub='payroll', label='Tax declarations', url_name='payroll:declarations', order=40, module='payroll', icon='file-invoice',
            keywords=('insurance', 'cit', 'verify')),
    NavItem(hub='payroll', label='Loans & advances', url_name='payroll:loans', order=50, module='payroll', icon='coins',
            keywords=('loan', 'advance', 'emi', 'instalment')),
    NavItem(hub='payroll', label='Reports', url_name='payroll:reports', order=60, module='payroll', icon='report-money',
            palette_label='Payroll reports', keywords=('tds', 'ssf', 'tax deposit', 'certificates', 'leave liability', 'pay variance')),
    NavItem(hub='payroll', label='Optimum CIT', url_name='payroll:optimum_cit', order=70, module='payroll', icon='bulb',
            keywords=('citizen investment trust', 'tax saving')),
    NavItem(hub='payroll', label='Held pay', url_name='payroll:held_pay', order=80, module='payroll', icon='hand-stop',
            keywords=('hold', 'release')),
    NavItem(hub='payroll', label='Opening balances', url_name='payroll:opening_balances', order=90, module='payroll', icon='scale',
            keywords=('previous employer', 'go live', 'year to date')),
    NavItem(hub='payroll', label='Tax & SSF rules', url_name='payroll:rules', order=100, module='payroll', icon='gavel',
            keywords=('tax slabs', 'rule versions', 'statutory')),
    NavItem(hub='payslips', label='Payslips', url_name='payroll:my_payslips', order=10, employee_only=True, icon='receipt-2',
            palette_label='My payslips', keywords=('salary', 'pay', 'certificate')),
    NavItem(hub='payslips', label='Tax savings', url_name='payroll:my_tax', order=20, employee_only=True, icon='receipt-tax',
            keywords=('tax', 'declaration', 'insurance', 'cit', 'donation')),
]
