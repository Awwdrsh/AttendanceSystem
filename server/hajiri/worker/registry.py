"""
Job and hook registry (the Django side of apps/api/src/module-jobs.ts). Apps register their background work in their
own `jobs.py` (and per-company seed data in `seeds.py`); the worker imports every `hajiri.<app>.jobs` and
`hajiri.<app>.seeds` module once, so no app edits a shared list:

    # hajiri/documents/jobs.py
    from hajiri.worker.registry import daily_job, periodic_job

    @daily_job('documents')                 # after the nightly sweep, once a day at 00:30 Nepal time
    def documents_daily(now): ...           # returns a JSON-able summary

    @periodic_job('kioskHeartbeat', every_seconds=300)
    def heartbeat(now): ...

    # hajiri/payroll/seeds.py
    from hajiri.worker.registry import company_seeder

    @company_seeder('payroll', order=10)
    def seed_payroll_defaults(company_id) -> dict[str, int]: ...

Hooks connect modules that would otherwise import each other (the leave accrual the worker runs monthly, workflow
approvers for SLA reminders):

    register_hook('leave.monthly_accrual', run_monthly_accrual)   # fn(company_id, bs_year, bs_month) -> dict
    register_hook('leave.catch_up', run_leave_catch_up)           # fn(company_id, today_iso) -> Any
    register_hook('workflow.approvers_for_step', approvers)       # fn(company_id, employee_id, step) -> list[user_id]
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Callable

from django.apps import apps

log = logging.getLogger('hajiri.worker')


@dataclass(frozen=True)
class Job:
    name: str
    fn: Callable[..., Any]
    every_seconds: int | None = None   # None: daily, after the nightly sweep


@dataclass(frozen=True)
class Seeder:
    name: str
    fn: Callable[[str], dict]
    order: int = 100


_DAILY: dict[str, Job] = {}
_PERIODIC: dict[str, Job] = {}
_SEEDERS: dict[str, Seeder] = {}
_HOOKS: dict[str, Callable[..., Any]] = {}
_discovered = False


def daily_job(name: str):
    def deco(fn):
        register_daily(name, fn)
        return fn
    return deco


def periodic_job(name: str, every_seconds: int):
    def deco(fn):
        register_periodic(name, fn, every_seconds)
        return fn
    return deco


def company_seeder(name: str, order: int = 100):
    def deco(fn):
        register_seeder(name, fn, order)
        return fn
    return deco


def register_daily(name: str, fn: Callable[[Any], Any]) -> None:
    """fn(now: datetime) runs once a day after the nightly sweep; a failure is logged and never stops the others."""
    _DAILY[name] = Job(name, fn)


def register_periodic(name: str, fn: Callable[[Any], Any], every_seconds: int) -> None:
    """fn(now: datetime) runs every `every_seconds` (at most once per 15 s worker tick)."""
    _PERIODIC[name] = Job(name, fn, every_seconds)


def register_seeder(name: str, fn: Callable[[str], dict], order: int = 100) -> None:
    """fn(company_id) adds a module's idempotent defaults for a company and returns counts of what it added."""
    _SEEDERS[name] = Seeder(name, fn, order)


def register_hook(name: str, fn: Callable[..., Any]) -> None:
    _HOOKS[name] = fn


def hook(name: str) -> Callable[..., Any] | None:
    autodiscover()
    return _HOOKS.get(name)


def autodiscover() -> None:
    """Import every app's jobs.py and seeds.py once."""
    global _discovered
    if _discovered:
        return
    _discovered = True
    for cfg in apps.get_app_configs():
        if not cfg.name.startswith('hajiri.'):
            continue
        for mod in ('jobs', 'seeds'):
            try:
                import_module(f'{cfg.name}.{mod}')
            except ModuleNotFoundError as e:
                if e.name != f'{cfg.name}.{mod}':
                    log.exception('could not import %s.%s', cfg.name, mod)
            except Exception:  # noqa: BLE001 - one broken app must not stop the worker
                log.exception('could not import %s.%s', cfg.name, mod)


def daily_jobs() -> list[Job]:
    autodiscover()
    return list(_DAILY.values())


def periodic_jobs() -> list[Job]:
    autodiscover()
    return list(_PERIODIC.values())


def seeders() -> list[Seeder]:
    autodiscover()
    return sorted(_SEEDERS.values(), key=lambda s: (s.order, s.name))
