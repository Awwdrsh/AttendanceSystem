"""Worker hooks: the monthly leave accrual and the daily catch-up (worker.ts step 2b)."""
from hajiri.worker.registry import register_hook

from .accrual import run_leave_catch_up, run_monthly_accrual

register_hook('leave.monthly_accrual', run_monthly_accrual)   # fn(company_id, bs_year, bs_month) -> summary dict
register_hook('leave.catch_up', run_leave_catch_up)           # fn(company_id, today_iso) -> summary dict
