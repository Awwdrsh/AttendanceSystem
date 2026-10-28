"""Gateway environment (port of the TS gateway's config). Read from the process environment, which Django settings have
already filled from server/.env, plus the gateway's own server/gateway.env."""
from __future__ import annotations

import os
from dataclasses import dataclass

from rest_framework import serializers

from hajiri.core.validation import choice_message

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


class PositiveIntEnv(serializers.Field):
    """z.coerce.number().int().positive(): Number('') is 0, so an empty value is "too small"."""
    default_error_messages = {
        'nan': 'Invalid input: expected number, received NaN',
        'int': 'Invalid input: expected int, received number',
        'small': 'Too small: expected number to be >0',
    }

    def to_internal_value(self, data) -> int:
        s = str(data).strip()
        try:
            n = float(s) if s else 0.0
        except ValueError:
            self.fail('nan')
        if not n.is_integer():
            self.fail('int')
        if n <= 0:
            self.fail('small')
        return int(n)

    def to_representation(self, value):
        return value


def _text(**kw) -> serializers.CharField:
    """An env string as given (no trimming); unset means the default."""
    return serializers.CharField(required=False, trim_whitespace=False, **kw)


class GatewayEnv(serializers.Serializer):
    """The gateway's environment, with the TS gateway's (zod) messages."""
    PORT = PositiveIntEnv(required=False, default=8080)
    LOG_LEVEL = serializers.ChoiceField(LOG_LEVELS, required=False, default='info', error_messages={
        'invalid_choice': choice_message(LOG_LEVELS), 'blank': choice_message(LOG_LEVELS)})
    DATABASE_URL = _text(error_messages={'blank': 'Too small: expected string to have >=1 characters'})
    GATEWAY_SIGNING_KEY = _text(allow_blank=True)
    DATA_ENCRYPTION_KEY = _text(min_length=40, error_messages={
        'min_length': 'Too small: expected string to have >=40 characters',
        'blank': 'Too small: expected string to have >=40 characters'})
    GATEWAY_MAX_PENDING_DEVICES = PositiveIntEnv(required=False, default=50)
    GATEWAY_MAX_CLOCK_SKEW_MIN = PositiveIntEnv(required=False, default=10)
    GATEWAY_RAW_DIR = _text(allow_blank=True, default='data/raw')
    GATEWAY_ALLOW_DANGEROUS_COMMANDS = serializers.ChoiceField(('true', 'false'), required=False, default='false', error_messages={
        'invalid_choice': choice_message(('true', 'false')), 'blank': choice_message(('true', 'false'))})


def load_config(env: dict | None = None) -> GatewayConfig:
    env = dict(os.environ if env is None else env)
    form = GatewayEnv(data={k: env[k] for k in GatewayEnv._declared_fields if k in env})
    if not form.is_valid():
        issues = [f'{name}: {msg}' for name, msgs in form.errors.items() for msg in msgs]
        raise ValueError(f'Invalid environment: {"; ".join(issues)}')
    v = form.validated_data
    return GatewayConfig(
        PORT=v['PORT'], DATABASE_URL=v.get('DATABASE_URL'), LOG_LEVEL=v['LOG_LEVEL'],
        GATEWAY_SIGNING_KEY=v.get('GATEWAY_SIGNING_KEY'), DATA_ENCRYPTION_KEY=v.get('DATA_ENCRYPTION_KEY'),
        GATEWAY_MAX_PENDING_DEVICES=v['GATEWAY_MAX_PENDING_DEVICES'], GATEWAY_MAX_CLOCK_SKEW_MIN=v['GATEWAY_MAX_CLOCK_SKEW_MIN'],
        GATEWAY_RAW_DIR=v['GATEWAY_RAW_DIR'], GATEWAY_ALLOW_DANGEROUS_COMMANDS=v['GATEWAY_ALLOW_DANGEROUS_COMMANDS'] == 'true',
    )


#: The keys the gateway reads (GatewayConfig fields plus NODE_ENV for the production start-up check).
GATEWAY_KEYS = tuple(GatewayConfig.__dataclass_fields__) + ('NODE_ENV',)


def gateway_env_files() -> list:
    """The gateway's own env file, server/gateway.env (what scripts/setup.sh writes: PORT=8080, GATEWAY_SIGNING_KEY,
    the shared DATA_ENCRYPTION_KEY ...)."""
    from django.conf import settings
    return [settings.BASE_DIR / 'gateway.env']


def _web_env_files() -> list:
    from django.conf import settings
    return [settings.BASE_DIR / '.env']


def gateway_environ(environ: dict | None = None, files: list | None = None, web_files: list | None = None) -> tuple[dict, dict]:
    """The gateway's environment: the process environment plus server/gateway.env (dotenv never overrides a variable
    the process already has).

    Django settings have already copied server/.env into os.environ; those values belong to the web
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
