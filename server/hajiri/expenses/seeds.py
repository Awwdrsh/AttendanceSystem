"""Per-company expense and travel defaults (port of apps/api/src/modules/expenses/seed.ts)."""
from __future__ import annotations

from hajiri.core.settings_store import add_setting_if_missing
from hajiri.db.models import ExpenseCategory, TadaRate, WorkflowDefinition
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


DEFAULT_POLICY = {'mileageRatePerKm': '15', 'limitMode': 'block', 'claimWindowDays': 90}
# Claims, trips and settlements are approved by the manager, then HR.
DEFAULT_CHAIN = [{'role': 'manager'}, {'role': 'hr'}]


def seed_expense_defaults(company_id: str) -> dict[str, int]:
    """Idempotent: categories, TADA rates, the manager -> HR approval chain for claims, trips and settlements, the policy.
    Rows the company already has (by their unique keys) are left alone."""
    company_id = str(company_id)
    have = set(ExpenseCategory.objects.for_company(company_id).values_list('code', flat=True))
    missing = [ExpenseCategory(company_id=company_id, code=code, name=name, kind=kind, receipt_required_above=receipt, sort_order=sort)
               for code, name, kind, receipt, sort in DEFAULT_CATEGORIES if code not in have]
    ExpenseCategory.objects.bulk_create(missing, ignore_conflicts=True)
    TadaRate.objects.bulk_create([TadaRate(company_id=company_id, grade='*', destination=dest, da_rate=da, lodging_rate=lodging)
                                  for dest, da, lodging in DEFAULT_TADA], ignore_conflicts=True)
    WorkflowDefinition.objects.bulk_create([WorkflowDefinition(company_id=company_id, object_type=t, steps=DEFAULT_CHAIN)
                                            for t in ('expense_claim', 'travel_request', 'travel_settlement')],
                                           ignore_conflicts=True)
    add_setting_if_missing(company_id, 'expense.policy', dict(DEFAULT_POLICY))
    return {'expenseCategories': len(missing)}


@company_seeder('expenses', order=40)
def _seed(company_id):
    return seed_expense_defaults(company_id)
