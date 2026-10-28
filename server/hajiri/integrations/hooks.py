"""
Links between modules that would otherwise import each other (port of apps/api/src/modules/integrations.ts). When a
candidate is hired, the person gets a "Joined" event on their career timeline and, when the company has auto-start
on, an onboarding checklist. Hiring calls run_hire_hooks inside the hire transaction; the people and lifecycle modules
provide the two steps as registry hooks:

    register_hook('people.record_career_event', fn)      # fn(*, company_id, employee_id, kind, effective_date, details, note, actor_id)
    register_hook('lifecycle.auto_start_onboarding', fn)  # fn(company_id, employee_id, join_date, actor_id)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from hajiri.db.models import Employee
from hajiri.worker.registry import hook


@dataclass(frozen=True)
class HireEvent:
    company_id: str
    employee_id: str
    application_id: str
    job_id: str
    join_date: str
    actor_id: str | None


_hire_hooks: list[Callable[[HireEvent], None]] = []
_wired = False


def on_candidate_hired(fn: Callable[[HireEvent], None]) -> Callable[[HireEvent], None]:
    _hire_hooks.append(fn)
    return fn


def off_candidate_hired(fn: Callable[[HireEvent], None]) -> None:
    """Remove a hook added with on_candidate_hired (tests use it to take their stand-ins away again)."""
    if fn in _hire_hooks:
        _hire_hooks.remove(fn)


def run_hire_hooks(event: HireEvent) -> None:
    wire_modules()
    for fn in list(_hire_hooks):
        fn(event)


def _joined(e: HireEvent) -> None:
    record = hook('people.record_career_event')
    if record:
        designation = (Employee.objects.for_company(e.company_id).filter(pk=e.employee_id)
                       .values_list('designation', flat=True).first())
        record(company_id=e.company_id, employee_id=e.employee_id, kind='hire', effective_date=e.join_date,
               details={'designation': designation} if designation else {}, note='Hired through the careers pipeline',
               actor_id=e.actor_id)
    onboard = hook('lifecycle.auto_start_onboarding')
    if onboard:
        onboard(e.company_id, e.employee_id, e.join_date, e.actor_id)


def wire_modules() -> None:
    global _wired
    if _wired:
        return
    _wired = True
    on_candidate_hired(_joined)
