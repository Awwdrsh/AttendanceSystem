"""`manage.py seed_bs_calendar`: fill the bs_calendar table from the engine's month table."""
from django.core.management.base import BaseCommand

from hajiri.core.bs_calendar_seed import seed_bs_calendar


class Command(BaseCommand):
    help = 'Upsert the Bikram Sambat calendar table.'

    def handle(self, *args, **o):
        self.stdout.write(f'bs_calendar: {seed_bs_calendar()} rows upserted')
