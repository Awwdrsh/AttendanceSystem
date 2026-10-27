"""
Public interface of the lifecycle app. Other slices import from here:

    from hajiri.lifecycle.services import auto_start_onboarding, lifecycle_policy, start_case, record_exit, my_tasks

- auto_start_onboarding(company_id, employee_id, join_date, actor_id) -> case id | None (inside the caller's transaction,
  behind a savepoint; also registered as the worker hook 'lifecycle.auto_start_onboarding').
- lifecycle_policy(company_id) -> {noticeDays, autoStartOnboarding, probationReminderDays, selfExitInterview}.
- start_case(...), record_exit(...), accept_exit(...), complete_exit(...), clearance_blockers(exit_id, employee_id),
  settlement_status(company_id, employee_id, lwd), lifecycle_daily(now) (daily job 'lifecycle').
- Route functions (one per TS route) are in hajiri.lifecycle.api: my_tasks(user), my_exits(user), my_assets(user), ...
Template fragments other pages embed (hx-get, hx-trigger="load", hx-swap="outerHTML"):
- 'lifecycle:my_checklist'  /_/lifecycle/my-checklist  Home card (MyChecklistCard.tsx); empty 200 when nothing to do.
- 'lifecycle:my_leaving'    /_/lifecycle/my-leaving    "Leaving the company" block on /me/help (MyTicketsPage Leaving).
"""
from .api import my_assets, my_exits, my_tasks  # noqa: F401
from .service import (EXIT_KINDS, accept_exit, auto_start_onboarding, clearance_blockers, complete_exit,  # noqa: F401
                      ensure_lifecycle_defaults, lifecycle_daily, lifecycle_policy, record_exit, settlement_status, start_case)
