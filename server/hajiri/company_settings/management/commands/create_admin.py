"""`manage.py create_admin --email a@b.c [--password ...|HAJIRI_PASSWORD] [--name Administrator] [--company DEMO]`.
The new admin must change the password at first sign-in."""
from django.core.management.base import BaseCommand, CommandError

from hajiri.core.auth import create_user
from hajiri.core.crypto import password_policy_error

from ._common import company_id, password_arg


class Command(BaseCommand):
    help = 'Create an admin user for a company.'

    def add_arguments(self, parser):
        parser.add_argument('--email')
        parser.add_argument('--password')
        parser.add_argument('--name', default='Administrator')
        parser.add_argument('--company', default='DEMO')

    def handle(self, *args, **o):
        email, password = o.get('email'), password_arg(o)
        if not email or not password:
            raise CommandError('--email and --password (or HAJIRI_PASSWORD) are required')
        err = password_policy_error(password)
        if err:
            raise CommandError(err)
        u = create_user(company_id=company_id(o['company']), email=email, name=o['name'], password=password,
                        roles=['admin'], must_change_password=True)
        self.stdout.write(f'admin user {u.email} created ({u.id})')
