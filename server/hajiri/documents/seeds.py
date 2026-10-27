"""Per-company defaults (seed-defaults.ts): document categories, built-in letter templates, the letter-request flow."""
from hajiri.worker.registry import company_seeder

from .letters import seed_document_defaults


@company_seeder('documents', order=60)
def seed_documents(company_id):
    out = seed_document_defaults(company_id)
    return {'documentCategories': out['categories'], 'letterTemplates': out['templates']}
