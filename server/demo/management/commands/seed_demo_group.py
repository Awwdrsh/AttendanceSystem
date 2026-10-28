"""`manage.py seed_demo_group --database <url> --yes [--company DEMO] [--sister DEMOSYS] [--sister-name ...]
[--group-name ...] [--no-suite] [--only a,b]`: a demo group of two companies (see demo.group), each filled by the demo
suite. The same safety rules as seed_demo_suite: the target is named on the command line and is never the live
database.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from .seed_demo_suite import split_sections, use_demo_database

NEED_TARGET = ('seed_demo_group adds sample companies and people. Name the demo database with --database <url> and add '
               '--yes to confirm.')


class Command(BaseCommand):
    help = 'Set up a demo group of two companies with shared people and fill both with sample data (never the live database).'

    def add_arguments(self, parser):
        from demo.group import DEFAULT_SISTER
        parser.add_argument('--database', dest='database', help='postgres:// URL of the demo database (required; never the live one)')
        parser.add_argument('--yes', action='store_true', help='confirm')
        parser.add_argument('--company', default='DEMO', help='the main company (DEMO is created when missing)')
        parser.add_argument('--sister', default=DEFAULT_SISTER[0], help='code of the sister company')
        parser.add_argument('--sister-name', dest='sister_name', default=DEFAULT_SISTER[1])
        parser.add_argument('--group-name', dest='group_name', help='name of the group (default: "<main company> group")')
        parser.add_argument('--no-suite', dest='no_suite', action='store_true', help='set up the group only, without sample records')
        parser.add_argument('--only', help='comma-separated demo-suite sections')

    def handle(self, *args, **o):
        use_demo_database(o, NEED_TARGET)
        from demo.group import seed_demo_group
        from demo.kit import ApiError
        try:
            out = seed_demo_group(company=o.get('company') or 'DEMO', sister=o.get('sister') or 'DEMOSYS',
                                  sister_name=o.get('sister_name') or 'Demo Systems Pvt. Ltd.', group_name=o.get('group_name'),
                                  suite=not o.get('no_suite'), only=split_sections(o.get('only')),
                                  log=lambda m: self.stdout.write(m))
        except (RuntimeError, ApiError) as e:
            raise CommandError(str(e)) from e
        names = ', '.join(f"{c['name']} ({c['code']})" for c in out.companies)
        self.stdout.write(f'group {out.group_name}: {names}; {len(out.created)} new setup steps')
        self.result = out
        if out.failed:
            raise CommandError(f"failed: {', '.join(f'{f['company']} {f['section']}' for f in out.failed)}")
