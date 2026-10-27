"""Per-company default (seed-defaults.ts): the hiring policy, 180 days' retention with the careers page on.

seed_defaults already writes 'hiring.policy' with its other settings and counts it there, as seed-defaults.ts does; this
seeder only makes sure the row exists and adds no count of its own (a 'settings' key here would overwrite that total)."""
import json

from hajiri.core.db import execute
from hajiri.worker.registry import company_seeder

DEFAULT_POLICY = {'retentionDays': 180, 'careersEnabled': True}


@company_seeder('hiring', order=70)
def seed_hiring(company_id):
    execute("INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'hiring.policy', %s) ON CONFLICT DO NOTHING",
                [company_id, json.dumps(DEFAULT_POLICY)])
    return {}
