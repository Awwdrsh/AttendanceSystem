"""Gateway environment (port of apps/gateway/src/config.ts). Read from the process environment, which Django settings
have already filled from server/.env and apps/api/.env."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

LOG_LEVELS = ('fatal', 'error', 'warn', 'info', 'debug', 'trace')


@dataclass(frozen=True)
class GatewayConfig:
    PORT: int = 8080
    DATABASE_URL: str | None = None
    LOG_LEVEL: str = 'info'
    GATEWAY_SIGNING_KEY: str | None = None
    #: Same key as the API (base64 of 32 bytes). Templates are discarded when absent; never stored in plaintext.
    DATA_ENCRYPTION_KEY: str | None = None
    #: Unknown terminals are parked as pending; beyond this many pending rows new ones are refused (plan §12.5).
    GATEWAY_MAX_PENDING_DEVICES: int = 50
    #: Punches further than this in the future are flagged clock_skew (plan §7.7, §12.5 skew window).
    GATEWAY_MAX_CLOCK_SKEW_MIN: int = 10
    #: Raw request archive directory (plan §8.2 "raw payload archive").
    GATEWAY_RAW_DIR: str = 'data/raw'
    #: CLEAR DATA / firmware commands are refused unless explicitly enabled. SHELL is always refused.
    GATEWAY_ALLOW_DANGEROUS_COMMANDS: bool = False


def _pos_int(env, name, default, issues) -> int:
    v = env.get(name)
    if v is None:
        return default
    s = v.strip()
    try:
        n = float(s) if s else 0.0     # z.coerce.number(): Number('') is 0
    except ValueError:
        issues.append(f'{name}: Invalid input: expected number, received NaN')
        return default
    if not n.is_integer():
        issues.append(f'{name}: Invalid input: expected int, received number')
    elif n <= 0:
        issues.append(f'{name}: Too small: expected number to be >0')
    return int(n) if n.is_integer() else default


def load_config(env: dict | None = None) -> GatewayConfig:
    env = dict(os.environ if env is None else env)
    issues: list[str] = []
    port = _pos_int(env, 'PORT', 8080, issues)
    level = env.get('LOG_LEVEL', 'info')
    if level not in LOG_LEVELS:
        issues.append('LOG_LEVEL: Invalid option: expected one of ' + '|'.join(f'"{x}"' for x in LOG_LEVELS))
    db_url = env.get('DATABASE_URL')
    if db_url is not None and len(db_url) < 1:
        issues.append('DATABASE_URL: Too small: expected string to have >=1 characters')
    dek = env.get('DATA_ENCRYPTION_KEY')
    if dek is not None and len(dek) < 40:
        issues.append('DATA_ENCRYPTION_KEY: Too small: expected string to have >=40 characters')
    pending = _pos_int(env, 'GATEWAY_MAX_PENDING_DEVICES', 50, issues)
    skew = _pos_int(env, 'GATEWAY_MAX_CLOCK_SKEW_MIN', 10, issues)
    dangerous = env.get('GATEWAY_ALLOW_DANGEROUS_COMMANDS', 'false')
    if dangerous not in ('true', 'false'):
        issues.append('GATEWAY_ALLOW_DANGEROUS_COMMANDS: Invalid option: expected one of "true"|"false"')
    if issues:
        raise ValueError(f'Invalid environment: {"; ".join(issues)}')
    return GatewayConfig(
        PORT=port, DATABASE_URL=db_url, LOG_LEVEL=level, GATEWAY_SIGNING_KEY=env.get('GATEWAY_SIGNING_KEY'),
        DATA_ENCRYPTION_KEY=dek, GATEWAY_MAX_PENDING_DEVICES=pending, GATEWAY_MAX_CLOCK_SKEW_MIN=skew,
        GATEWAY_RAW_DIR=env.get('GATEWAY_RAW_DIR', 'data/raw'), GATEWAY_ALLOW_DANGEROUS_COMMANDS=dangerous == 'true',
    )




#: The keys the gateway reads (GatewayConfig fields plus NODE_ENV for the production start-up check).
GATEWAY_KEYS = tuple(GatewayConfig.__dataclass_fields__) + ('NODE_ENV',)


def gateway_env_files() -> list:
    """The gateway's own env files, first wins: server/gateway.env, then apps/gateway/.env (what scripts/setup.sh
    writes: PORT=8080, GATEWAY_SIGNING_KEY, the shared DATA_ENCRYPTION_KEY ...)."""
    from django.conf import settings
    base = settings.BASE_DIR
    return [base / 'gateway.env', base.parent / 'apps' / 'gateway' / '.env']


def _web_env_files() -> list:
    from django.conf import settings
    base = settings.BASE_DIR
    return [base / '.env', base.parent / 'apps' / 'api' / '.env']


def gateway_environ(environ: dict | None = None, files: list | None = None, web_files: list | None = None) -> tuple[dict, dict]:
    """The environment the TS gateway saw: the process environment plus apps/gateway/.env (dotenv never overrides a
    variable the process already has).

    Django settings have already copied server/.env and apps/api/.env into os.environ; those values belong to the web
    app (PORT there is the API's port), so for the gateway's keys a value from the gateway's own env file wins over
    one that only came from a web env file, and a web-only PORT is dropped so the gateway keeps its 8080 default.
    Returns (env, origin) where origin maps a key taken from a gateway env file to that file's directory."""
    from dotenv import dotenv_values
    env = dict(os.environ if environ is None else environ)
    web: dict[str, str] = {}
    for f in reversed(web_files if web_files is not None else _web_env_files()):
        if f.is_file():
            web.update({k: v for k, v in dotenv_values(f).items() if v is not None})
    from_web_file = {k for k in GATEWAY_KEYS if k in env and k in web and env[k] == web[k]}
    origin: dict[str, object] = {}
    for f in reversed(files if files is not None else gateway_env_files()):
        if not f.is_file():
            continue
        for k, v in dotenv_values(f).items():
            if k in GATEWAY_KEYS and v is not None and (k not in env or k in from_web_file or k in origin):
                env[k] = v
                origin[k] = f.parent
    if 'PORT' in from_web_file and 'PORT' not in origin:
        env.pop('PORT', None)
    return env, origin
