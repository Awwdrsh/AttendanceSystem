"""The demo suite (port of apps/api/src/db/demo-suite/index.ts): realistic sample data for every HR module of one company.

Sections run in order and each runs once per company (recorded under company_setting 'demo.suite'): a section that
finished before is skipped, so running again only retries what failed. Each section runs in one transaction, so a
section that fails leaves nothing behind. Payroll runs are never touched.
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Callable

from django.db import transaction

from hajiri.core.settings_store import get_setting, put_setting
from hajiri.features.service import feature_on

from .context import DemoContext, build_context
from .engage import seed_engage
from .kit import ApiError, Kit
from .money import seed_expenses, seed_tax_savings
from .people import seed_career, seed_profiles, seed_structure
from .records import seed_assets, seed_documents, seed_helpdesk, seed_lifecycle
from .talent import seed_hiring, seed_learning, seed_performance
from .work import seed_field, seed_roster, seed_time

SECTIONS: list[tuple[str, Callable[[DemoContext], None]]] = [
    ('structure', seed_structure),
    ('profiles', seed_profiles),
    ('career', seed_career),
    ('documents', seed_documents),
    ('assets', seed_assets),
    ('lifecycle', seed_lifecycle),
    ('helpdesk', seed_helpdesk),
    ('time', seed_time),
    ('field', seed_field),
    ('roster', seed_roster),
    ('expenses', seed_expenses),
    ('tax-savings', seed_tax_savings),
    ('performance', seed_performance),
    ('learning', seed_learning),
    ('hiring', seed_hiring),
    ('engage', seed_engage),
]
DEMO_SUITE_SECTIONS = [name for name, _ in SECTIONS]
DEMO_SUITE_KEY = 'demo.suite'      # company_setting: the sections done, in order

# A module switched off under Settings, Features answers 404 "feature off", as its API routes did for the TS seeder
# (app.ts featureGate). Only gated routes count: the roster section uses /shifts and /field/swaps, which the field gate
# leaves open (it covers /field/breaks, /visits, /kiosks, /kiosk-pin and /kiosk-qr), so it runs with field work off.
SECTION_FEATURES: dict[str, tuple[str, ...]] = {
    'documents': ('documents',), 'assets': ('lifecycle',), 'lifecycle': ('lifecycle',), 'helpdesk': ('helpdesk',), 'time': ('time',),
    'field': ('field',), 'expenses': ('expenses',), 'performance': ('performance',), 'learning': ('learning',),
    'hiring': ('hiring',), 'engage': ('kudos', 'surveys', 'suggestions'),
}


@dataclass
class SuiteResult:
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)


def seed_demo_suite(*, company: str, only: list[str] | None = None, log: Callable[[str], None] = print) -> SuiteResult:
    """Fill every HR module with realistic sample data for one company, through the same services the pages use.
    Idempotent: a section that finished before is skipped."""
    kit = Kit()
    kit.start()
    out = SuiteResult()
    try:
        ctx = build_context(kit, company, log)
        order = list((get_setting(ctx.company_id, DEMO_SUITE_KEY) or {}).get('done') or [])
        finished = set(order)
        for name, fn in SECTIONS:
            if only and name not in only:
                continue
            if name in finished:
                out.skipped.append(name)
                continue
            try:
                with transaction.atomic():
                    off = [f for f in SECTION_FEATURES.get(name, ()) if not feature_on(ctx.company_id, f)]
                    if off:
                        raise ApiError(404, {'error': 'feature off', 'feature': off[0]}, name)
                    fn(ctx)
                    finished.add(name)
                    order.append(name)
                    put_setting(ctx.company_id, DEMO_SUITE_KEY, {'done': order})
                out.done.append(name)
                log(f'  ✓ {name}')
            except Exception as e:  # noqa: BLE001 - every section failure is reported, then the next section runs
                msg = str(e) if isinstance(e, (ApiError, RuntimeError)) else ''.join(traceback.format_exception(e))
                out.failed.append({'section': name, 'error': msg})
                log(f'  ✗ {name}: {msg}')
    finally:
        kit.stop()
    return out
