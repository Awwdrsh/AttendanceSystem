"""`manage.py seed_defaults [--company DEMO]`: leave types, workflows, settings and every module's defaults (idempotent)."""
from django.core.management.base import BaseCommand

from hajiri.company_settings.seed import seed_defaults

from ._common import company_id


class Command(BaseCommand):
    help = 'Add the default leave types, workflows, settings and module defaults for a company.'

    def add_arguments(self, parser):
        parser.add_argument('--company', default='DEMO')

    def handle(self, *args, **o):
        self.stdout.write(f'defaults seeded: {seed_defaults(company_id(o["company"]))}')
