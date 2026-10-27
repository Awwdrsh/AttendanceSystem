"""`manage.py reset_password --email a@b.c [--password ...|HAJIRI_PASSWORD]`: clears the lockout and signs out everywhere."""
from django.core.management.base import BaseCommand, CommandError

from hajiri.core.errors import HttpError
from hajiri.core.users import reset_password

from ._common import password_arg


class Command(BaseCommand):
    help = "Reset a user's password and revoke their sessions."

    def add_arguments(self, parser):
        parser.add_argument('--email')
        parser.add_argument('--password')

    def handle(self, *args, **o):
        email, password = o.get('email'), password_arg(o)
        if not email or not password:
            raise CommandError('--email and --password (or HAJIRI_PASSWORD) are required')
        try:
            reset_password(email, password)
        except HttpError as e:
            raise CommandError(e.message)
        self.stdout.write('password reset; all sessions revoked')
