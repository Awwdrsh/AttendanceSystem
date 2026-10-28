"""CORS for the JSON API: CORS_ORIGINS lists other origins (comma-separated, from settings or the environment) that may
call it with credentials, as the TS API's corsOrigins option."""
from __future__ import annotations

import os

from django.conf import settings


def cors_origins() -> tuple[str, ...]:
    """The allowed origins from settings.CORS_ORIGINS, else the CORS_ORIGINS environment variable."""
    raw = getattr(settings, 'CORS_ORIGINS', None)
    if raw is None:
        raw = os.environ.get('CORS_ORIGINS', '')
    items = raw.split(',') if isinstance(raw, str) else list(raw)
    return tuple(o.strip() for o in items if o and o.strip())


def cors_headers(origin: str) -> dict[str, str]:
    """The CORS response headers for an allowed origin."""
    return {'Access-Control-Allow-Origin': origin, 'Vary': 'Origin', 'Access-Control-Allow-Credentials': 'true',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
            'Access-Control-Allow-Methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS'}
