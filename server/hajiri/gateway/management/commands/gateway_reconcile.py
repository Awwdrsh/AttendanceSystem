"""Nightly reconciliation: queue a DATA QUERY ATTLOG for yesterday (or --date) on active ADMS terminals.

    manage.py gateway_reconcile --all
    manage.py gateway_reconcile --sn DEMO-01 --date 2026-09-21
Prints one JSON line per device.
"""
from __future__ import annotations

import json
import time

from django.core.management.base import BaseCommand

from engines.attendance import local_date_of
from hajiri.gateway.config import gateway_environ, load_config
from hajiri.gateway.reconcile import reconcile_day
from hajiri.gateway.store import PostgresStore


class Command(BaseCommand):
    help = "Ask ADMS terminals to re-send a day's ATTLOG so gaps are filled by the idempotent ingest."

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true', help='every active ADMS device')
        parser.add_argument('--sn', default='')
        parser.add_argument('--date', help='YYYY-MM-DD (default: yesterday in Nepal)')

    def handle(self, *args, **opts):
        cfg = load_config(gateway_environ()[0])
        store = PostgresStore(allow_dangerous_commands=cfg.GATEWAY_ALLOW_DANGEROUS_COMMANDS)
        date = opts['date'] or local_date_of(time.time() * 1000 - 86_400_000)
        sns = [d.serial_no for d in store.list_devices(status='active', protocol='adms')] if opts['all'] else [opts['sn']]
        for sn in filter(None, sns):
            self.stdout.write(json.dumps(reconcile_day(store, sn, date), separators=(',', ':')))
