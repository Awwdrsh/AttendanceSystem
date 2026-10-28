"""The gateway instance the views serve (port of apps/gateway/src/server.ts start-up). Built once from the
environment on first use, or set explicitly with configure() (tests, the run_gateway command)."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .app import Gateway
from .archive import RawArchive
from .config import gateway_environ, load_config
from .punch import Signer, TemplateCipher
from .store import MemoryStore, PostgresStore

log = logging.getLogger('hajiri.gateway')

_lock = threading.Lock()
_gateway: Gateway | None = None

_LEVELS = {'fatal': logging.CRITICAL, 'error': logging.ERROR, 'warn': logging.WARNING, 'info': logging.INFO,
           'debug': logging.DEBUG, 'trace': logging.DEBUG}


def configure(gw: Gateway | None) -> None:
    global _gateway
    with _lock:
        _gateway = gw


def current() -> Gateway:
    global _gateway
    if _gateway is None:
        with _lock:
            if _gateway is None:
                _gateway = build_gateway()
    return _gateway


def build_gateway(env: dict | None = None, *, memory: bool = False) -> Gateway:
    """Build the gateway from `env`, or by default from the process environment plus the gateway's own env file
    (server/gateway.env, see config.gateway_environ)."""
    if env is None:
        env, origin = gateway_environ()
    else:
        env, origin = dict(env), {}
    cfg = load_config(env)
    log.setLevel(_LEVELS[cfg.LOG_LEVEL])
    # An ephemeral key makes every stored punch signature unverifiable after a restart; never acceptable in production.
    if not cfg.GATEWAY_SIGNING_KEY and env.get('NODE_ENV') == 'production':
        raise ImproperlyConfigured('GATEWAY_SIGNING_KEY is required when NODE_ENV=production (run scripts/setup.sh)')
    signer = Signer.from_base64_pkcs8(cfg.GATEWAY_SIGNING_KEY) if cfg.GATEWAY_SIGNING_KEY else Signer.ephemeral_key()
    if signer.ephemeral:
        log.warning('GATEWAY_SIGNING_KEY not set: using an ephemeral signing key (dev only)')
    cipher = TemplateCipher.from_base64(cfg.DATA_ENCRYPTION_KEY) if cfg.DATA_ENCRYPTION_KEY else None
    if not cipher:
        log.warning('DATA_ENCRYPTION_KEY not set: biometric templates will be discarded, not stored')
    opts = dict(max_pending_devices=cfg.GATEWAY_MAX_PENDING_DEVICES, allow_dangerous_commands=cfg.GATEWAY_ALLOW_DANGEROUS_COMMANDS)
    if memory:
        store = MemoryStore(**opts)
        d = store.add_device('DEMO-01')
        store.mappings[f'{d.id}:1'] = 'demo-employee-1'
        log.warning('in-memory store with device DEMO-01 (PIN 1 mapped); nothing is saved')
    else:
        store = PostgresStore(**opts)
    raw_dir = Path(cfg.GATEWAY_RAW_DIR)
    if not raw_dir.is_absolute():
        # Relative to the env file that set it, otherwise to server/.
        raw_dir = Path(origin.get('GATEWAY_RAW_DIR') or settings.BASE_DIR) / raw_dir
    return Gateway(store=store, signer=signer, cipher=cipher, archive=RawArchive(str(raw_dir.resolve())),
                   max_clock_skew_min=cfg.GATEWAY_MAX_CLOCK_SKEW_MIN)
