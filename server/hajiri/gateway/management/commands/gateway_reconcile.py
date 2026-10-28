"""Nightly reconciliation: queue a DATA QUERY ATTLOG for yesterday (or --date) on active ADMS terminals.

    manage.py gateway_reconcile --all
    manage.py gateway_reconcile --sn DEMO-01 --date 2026-09-21
    manage.py gateway_reconcile --all --company BETA        # only one company's terminals (code or id)
Prints one JSON line per device.
"""
from __future__ import annotations

import json
import time

from django.core.management.base import BaseCommand, CommandError

from engines.attendance import local_date_of
from hajiri.gateway.config import gateway_environ, load_config
from hajiri.gateway.reconcile import reconcile_day
from hajiri.core.scoping import is_uuid
from hajiri.db.models import Company, Device
from hajiri.gateway.store import PostgresStore


class Command(BaseCommand):
    help = "Ask ADMS terminals to re-send a day's ATTLOG so gaps are filled by the idempotent ingest."

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true', help='every active ADMS device')
        parser.add_argument('--sn', default='')
        parser.add_argument('--date', help='YYYY-MM-DD (default: yesterday in Nepal)')
        parser.add_argument('--company', default='', help='with --all: only this company (code or id)')

    def handle(self, *args, **opts):
        cfg = load_config(gateway_environ()[0])
        store = PostgresStore(allow_dangerous_commands=cfg.GATEWAY_ALLOW_DANGEROUS_COMMANDS)
        date = opts['date'] or local_date_of(time.time() * 1000 - 86_400_000)
        company_id = self._company_id(opts['company']) if opts['company'] else None
        if opts['all']:
            devices = store.list_devices(status=Device.Status.ACTIVE, protocol=Device.Protocol.ADMS, company_id=company_id)
            sns = [d.serial_no for d in devices]
        else:
            sns = [opts['sn']]
        for sn in filter(None, sns):
            self.stdout.write(json.dumps(reconcile_day(store, sn, date), separators=(',', ':')))

    @staticmethod
    def _company_id(ref: str) -> str:
        lookup = {'pk': ref} if is_uuid(ref) else {'code__iexact': ref}
        cid = Company.objects.filter(**lookup).values_list('id', flat=True).first()
        if cid is None:
            raise CommandError(f'company not found: {ref}')
        return str(cid)
