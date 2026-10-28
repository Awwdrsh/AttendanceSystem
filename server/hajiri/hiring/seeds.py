"""Per-company default (seed-defaults.ts): the hiring policy, 180 days' retention with the careers page on.

seed_defaults already writes 'hiring.policy' with its other settings and counts it there, as seed-defaults.ts does; this
seeder only makes sure the row exists and adds no count of its own (a 'settings' key here would overwrite that total)."""
from hajiri.worker.registry import company_seeder

from .services import DEFAULT_POLICY, seed_policy  # noqa: F401  (DEFAULT_POLICY re-exported)


@company_seeder('hiring', order=70)
def seed_hiring(company_id):
    seed_policy(company_id)
    return {}
