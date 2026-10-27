"""`manage.py seed_demo_suite --database <url> --yes [--company DEMO] [--only a,b]` (port of the TS CLI's seed-demo-suite).

Sample data for every HR module. The target must be named on the command line and must not be the live database (the
DATABASE_URL in the environment, server/.env or apps/api/.env): a mistyped command can never fill production with demo
people.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from dotenv import dotenv_values

NEED_TARGET = 'seed_demo_suite adds sample people data. Name the demo database with --database <url> and add --yes to confirm.'
REFUSE_LIVE = 'refusing to add demo data to the live database (the DATABASE_URL in server/.env, apps/api/.env or the environment)'


def live_env_files() -> list[Path]:
    base = Path(settings.BASE_DIR)
    return [base / '.env', base.parent / 'apps' / 'api' / '.env']


def live_database_urls(environ: dict | None = None, files: list[Path] | None = None) -> list[str]:
    """Every DATABASE_URL that names the live database: the process environment and each env file Django reads."""
    env = os.environ if environ is None else environ
    urls = [env['DATABASE_URL']] if env.get('DATABASE_URL') else []
    for f in live_env_files() if files is None else files:
        try:
            v = dotenv_values(f).get('DATABASE_URL') if f.is_file() else None
        except OSError:
            v = None
        if v:
            urls.append(v)
    return urls


def _key(url: str) -> tuple:
    """What makes two URLs the same database: host, port and database name (user, password and options aside)."""
    u = urlparse(url.strip())
    if u.scheme not in ('postgres', 'postgresql'):
        raise CommandError(f'--database must be a postgres:// URL, got {u.scheme or "nothing"}://')
    host = (u.hostname or 'localhost').lower()
    if host in ('127.0.0.1', '::1'):
        host = 'localhost'
    return host, u.port or 5432, unquote(u.path.lstrip('/').rstrip('/')).lower()


def is_live(target: str, live_urls: list[str]) -> bool:
    norm = target.strip().rstrip('/').lower()
    t = _key(target)
    for live in live_urls:
        if live.strip().rstrip('/').lower() == norm:
            return True
        try:
            if _key(live) == t:
                return True
        except CommandError:
            continue
    return False


def use_database(url: str) -> None:
    """Point Django's default connection at the demo database for the rest of this process."""
    from config.settings import database_from_url
    target = database_from_url(url)
    conn = connections['default']
    cur = conn.settings_dict
    same = all(str(cur.get(k) or '') == str(target[k] or '') for k in ('NAME', 'USER', 'HOST', 'PORT'))
    if same:
        return
    if conn.in_atomic_block:
        raise CommandError('cannot switch databases inside a transaction')
    conn.close()
    for k in ('NAME', 'USER', 'PASSWORD', 'HOST', 'PORT', 'OPTIONS'):
        cur[k] = target[k]


class Command(BaseCommand):
    help = 'Fill every HR module of a demo database with realistic sample data (never the live database).'

    def add_arguments(self, parser):
        parser.add_argument('--database', dest='database', help='postgres:// URL of the demo database (required; never the live one)')
        parser.add_argument('--yes', action='store_true', help='confirm')
        parser.add_argument('--company', default='DEMO')
        parser.add_argument('--only', help='comma-separated sections')

    def handle(self, *args, **o):
        target = o.get('database')
        if not target or not o.get('yes'):
            raise CommandError(NEED_TARGET)
        if is_live(target, live_database_urls()):
            raise CommandError(REFUSE_LIVE)
        use_database(target)
        from demo.suite import seed_demo_suite
        only = [s.strip() for s in (o.get('only') or '').split(',') if s.strip()] or None
        try:
            out = seed_demo_suite(company=o.get('company') or 'DEMO', only=only, log=lambda m: self.stdout.write(m))
        except RuntimeError as e:
            raise CommandError(str(e)) from e
        self.stdout.write(f"done: {', '.join(out.done) or 'nothing new'}; already there: {', '.join(out.skipped) or 'none'}")
        self.result = out
        if out.failed:
            raise CommandError(f"failed: {', '.join(f['section'] for f in out.failed)}")
