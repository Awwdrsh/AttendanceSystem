"""Run the device gateway (ZKTeco ADMS push and Hikvision ISAPI push) as its own process, default port 8080.

    manage.py run_gateway [--port 8080] [--host 0.0.0.0] [--threads 8] [--memory] [--check]

Port of apps/gateway/src/server.ts: same environment (PORT, GATEWAY_SIGNING_KEY, DATA_ENCRYPTION_KEY,
GATEWAY_MAX_PENDING_DEVICES, GATEWAY_MAX_CLOCK_SKEW_MIN, GATEWAY_RAW_DIR, GATEWAY_ALLOW_DANGEROUS_COMMANDS, LOG_LEVEL).
Served by waitress (pure Python), which stops cleanly on SIGINT / SIGTERM.
"""
from __future__ import annotations

import logging

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError

from hajiri.gateway import runtime
from hajiri.gateway.config import gateway_environ, load_config
from hajiri.gateway.wsgi import application


class Command(BaseCommand):
    help = 'Run the device gateway (ADMS and ISAPI endpoints) on its own port.'

    def add_arguments(self, parser):
        parser.add_argument('--port', type=int, help='listen port (default: PORT or 8080)')
        parser.add_argument('--host', default='0.0.0.0')
        parser.add_argument('--threads', type=int, default=8)
        parser.add_argument('--memory', action='store_true',
                            help='in-memory store with device DEMO-01 (PIN 1 mapped); nothing is saved')
        parser.add_argument('--check', action='store_true', help='build the gateway from the environment and exit')

    def handle(self, *args, **opts):
        try:
            cfg = load_config(gateway_environ()[0])
            gw = runtime.build_gateway(memory=opts['memory'])
        except (ImproperlyConfigured, ValueError) as e:
            raise CommandError(str(e)) from e
        log = logging.getLogger('hajiri.gateway')
        if not log.handlers and not logging.getLogger().handlers:
            logging.basicConfig(level=logging.INFO)
        runtime.configure(gw)
        if opts['check']:
            self.stdout.write(f"gateway ok: signer {gw.health()['signer']}, templates {gw.health()['templates']}, "
                              f"raw archive {gw.archive.dir}")
            return
        from waitress import serve
        port = opts['port'] or cfg.PORT
        log.info('hajiri device gateway listening %s', {'port': port, 'rawDir': gw.archive.dir})
        serve(application(), host=opts['host'], port=port, threads=opts['threads'], ident=None,
              max_request_body_size=11 * 1024 * 1024)
