import os

from django.core.management.base import CommandError

from hajiri.core.db import one


def company_id(code: str) -> str:
    r = one('SELECT id FROM company WHERE code = %s', [code])
    if not r:
        raise CommandError(f'company {code} not found (run create_company first)')
    return str(r['id'])


def password_arg(opts) -> str | None:
    """--password, or HAJIRI_PASSWORD (keeps it out of the process list)."""
    return opts.get('password') or os.environ.get('HAJIRI_PASSWORD')
