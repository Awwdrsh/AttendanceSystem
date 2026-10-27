"""`manage.py seed_demo --database-name <name> --yes`: the development seed (one company DEMO, a branch, a shift,
two employees and a ZKTeco device mapped to them), then BS calendar and defaults. Refuses the configured database
unless it is named explicitly, so a mistyped command never puts demo people in production."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from hajiri.company_settings.demo import seed_demo


class Command(BaseCommand):
    help = 'Add the small development demo data set.'

    def add_arguments(self, parser):
        parser.add_argument('--database-name', help='must equal the configured database name, to confirm the target')
        parser.add_argument('--yes', action='store_true')

    def handle(self, *args, **o):
        name = settings.DATABASES['default']['NAME']
        if not o['yes'] or o.get('database_name') != name:
            raise CommandError(f'seed_demo adds sample people. Confirm with --database-name {name} --yes.')
        self.stdout.write(f'demo data seeded: {seed_demo()}')
