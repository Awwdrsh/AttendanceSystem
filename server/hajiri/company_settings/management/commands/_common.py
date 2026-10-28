import os

from django.core.management.base import CommandError

from hajiri.db.models import Company


def company_id(code: str) -> str:
    cid = Company.objects.filter(code=code).values_list('id', flat=True).first()
    if not cid:
        raise CommandError(f'company {code} not found (run create_company first)')
    return str(cid)


def password_arg(opts) -> str | None:
    """--password, or HAJIRI_PASSWORD (keeps it out of the process list)."""
    return opts.get('password') or os.environ.get('HAJIRI_PASSWORD')
