"""Small helpers the Django-stack scripts need, so they depend on nothing but the server's Python (no node, no psql).

    server/.venv/bin/python scripts/django_ops.py <command> [args]

    secret N                    N random bytes, base64 (DATA_ENCRYPTION_KEY is 32, PASSWORD_PEPPER 24, as setup.sh made them)
    django-secret               a DJANGO_SECRET_KEY
    signing-key                 a new GATEWAY_SIGNING_KEY (base64 Ed25519 PKCS#8 DER)
    password                    a temporary admin password (12 random bytes, base64url)
    wait-db [SECONDS]           wait until the configured database answers (default 30 s); exit 1 if it never does
    db-name                     the configured database name (seed_demo wants it as --database-name)
    ensure-company CODE NAME    create the company and its HQ branch if missing (what setup.sh did with psql)
    has-user EMAIL              exit 0 when a user with this email exists, 1 otherwise
    has-company CODE            exit 0 when the company exists, 1 otherwise
    health                      container health check: GET /health of this container's service (HAJIRI_SERVICE=web
                                on PORT, gateway on GATEWAY_PORT); for the worker, that the database answers
"""
from __future__ import annotations

import base64
import os
import secrets
import sys
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / 'server'


def _django() -> None:
    sys.path.insert(0, str(SERVER))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    # A blank optional setting (Docker Compose passes unset .env values as '') falls back to its default: Django refuses
    # an empty SECRET_KEY, and an empty ALLOWED_HOSTS would refuse every request.
    for name in ('DJANGO_SECRET_KEY', 'ALLOWED_HOSTS'):
        if os.environ.get(name) == '':
            del os.environ[name]
    import django
    django.setup()


def secret(n: int) -> str:
    return base64.b64encode(secrets.token_bytes(n)).decode()


def signing_key() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    der = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption())
    return base64.b64encode(der).decode()


def wait_db(seconds: float) -> bool:
    _django()
    from django.db import connection
    from django.db.utils import OperationalError
    deadline = time.monotonic() + seconds
    while True:
        try:
            connection.ensure_connection()
            return True
        except OperationalError:
            connection.close()
            if time.monotonic() >= deadline:
                return False
            time.sleep(1)


def ensure_company(code: str, name: str) -> str:
    import re
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,20}', code):
        raise SystemExit('--company must be 1-20 letters, digits, _ or -')
    _django()
    from hajiri.core.db import execute, one
    execute('INSERT INTO company (code, name_en) VALUES (%s, %s) ON CONFLICT (code) DO NOTHING', [code, name])
    execute("""INSERT INTO branch (company_id, code, name_en) SELECT id, 'HQ', 'Head Office' FROM company WHERE code = %s
               ON CONFLICT DO NOTHING""", [code])
    return str(one('SELECT id FROM company WHERE code = %s', [code])['id'])


def health() -> int:
    import urllib.request
    svc = os.environ.get('HAJIRI_SERVICE', 'web')
    if svc == 'worker':
        return 0 if wait_db(3) else 1
    port = os.environ.get('GATEWAY_PORT', '8080') if svc == 'gateway' else os.environ.get('PORT', '3000')
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=4) as r:
            return 0 if r.status == 200 else 1
    except OSError:
        return 1


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__.strip())
        return 0 if argv else 1
    cmd, args = argv[0], argv[1:]
    if cmd == 'secret':
        print(secret(int(args[0]) if args else 32))
    elif cmd == 'django-secret':
        print(secrets.token_urlsafe(50))
    elif cmd == 'signing-key':
        print(signing_key())
    elif cmd == 'password':
        print(secrets.token_urlsafe(12))
    elif cmd == 'wait-db':
        return 0 if wait_db(float(args[0]) if args else 30) else 1
    elif cmd == 'health':
        return health()
    elif cmd == 'db-name':
        _django()
        from django.conf import settings
        print(settings.DATABASES['default']['NAME'])
    elif cmd == 'ensure-company':
        if len(args) != 2:
            print('usage: ensure-company CODE NAME', file=sys.stderr)
            return 1
        print(ensure_company(args[0], args[1]))
    elif cmd in ('has-user', 'has-company'):
        if len(args) != 1:
            print(f'usage: {cmd} VALUE', file=sys.stderr)
            return 1
        _django()
        from hajiri.core.db import one
        sql = 'SELECT 1 AS x FROM app_user WHERE email = lower(%s)' if cmd == 'has-user' else 'SELECT 1 AS x FROM company WHERE code = %s'
        return 0 if one(sql, [args[0]]) else 1
    else:
        print(f'unknown command {cmd}\n\n{__doc__.strip()}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
