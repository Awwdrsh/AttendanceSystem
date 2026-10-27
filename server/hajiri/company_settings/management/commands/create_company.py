"""Create a company and seed its defaults: `manage.py create_company --code ACME --name "Acme Pvt. Ltd."`."""
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError

from hajiri.company_settings.seed import create_company


class Command(BaseCommand):
    help = 'Create a company with its default leave types, workflows and settings.'

    def add_arguments(self, parser):
        parser.add_argument('--code', required=True)
        parser.add_argument('--name', required=True)
        parser.add_argument('--name-ne')
        parser.add_argument('--no-seed', action='store_true')

    def handle(self, *args, **o):
        try:
            c = create_company(o['code'], o['name'], o.get('name_ne'), seed=not o['no_seed'])
        except IntegrityError:
            raise CommandError(f'company {o["code"]} already exists')
        self.stdout.write(f'company {c["code"]} created ({c["id"]}); defaults: {c["seeded"]}')
