"""Per-company expense and travel defaults (port of apps/api/src/modules/expenses/seed.ts)."""
from __future__ import annotations

from hajiri.core.db import execute, one
from hajiri.worker.registry import company_seeder

# code, name, kind, receipt required above (None = never), sort order
DEFAULT_CATEGORIES = (
    ('LOCAL_TRAVEL', 'Local travel', 'amount', '500', 10),
    ('MEALS', 'Meals', 'amount', '500', 20),
    ('FUEL', 'Fuel (own vehicle)', 'mileage', None, 30),
    ('MOBILE', 'Mobile & internet', 'amount', '0', 40),
    ('MEDICAL', 'Medical', 'amount', '0', 50),
    ('OFFICE', 'Office supplies', 'amount', '0', 60),
    ('OTHER', 'Other', 'amount', '0', 70),
)

# Default daily allowance and lodging per night ('*' = every grade); international figures are US dollars.
DEFAULT_TADA = (('valley', '1000', '2500'), ('city', '1500', '3000'), ('rural', '1200', '2000'), ('international', '50', '100'))


def seed_expense_defaults(company_id: str) -> dict[str, int]:
    """Idempotent: categories, TADA rates, the manager -> HR approval chain for claims, trips and settlements, the policy."""
    company_id = str(company_id)
    n = 0
    for code, name, kind, receipt, sort in DEFAULT_CATEGORIES:
        r = one("""INSERT INTO expense_category (company_id, code, name, kind, receipt_required_above, sort_order)
                   VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (company_id, code) DO NOTHING RETURNING id""",
                [company_id, code, name, kind, receipt, sort])
        n += 1 if r else 0
    for dest, da, lodging in DEFAULT_TADA:
        execute("""INSERT INTO tada_rate (company_id, grade, destination, da_rate, lodging_rate) VALUES (%s, '*', %s, %s, %s)
                   ON CONFLICT (company_id, grade, destination) DO NOTHING""", [company_id, dest, da, lodging])
    for t in ('expense_claim', 'travel_request', 'travel_settlement'):
        execute("""INSERT INTO workflow_definition (company_id, object_type, steps) VALUES (%s, %s, '[{"role":"manager"},{"role":"hr"}]')
                   ON CONFLICT (company_id, object_type) DO NOTHING""", [company_id, t])
    execute("""INSERT INTO company_setting (company_id, key, value)
               VALUES (%s, 'expense.policy', '{"mileageRatePerKm": "15", "limitMode": "block", "claimWindowDays": 90}') ON CONFLICT DO NOTHING""",
            [company_id])
    return {'expenseCategories': n}


@company_seeder('expenses', order=40)
def _seed(company_id):
    return seed_expense_defaults(company_id)
