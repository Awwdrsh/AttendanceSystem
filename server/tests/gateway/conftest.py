"""Gateway test helpers: the gateway URLconf with no middleware (as run_gateway serves it) and a small client that
plays the part of supertest's request(app)."""
from __future__ import annotations

import json
import time

import pytest
from django.test import Client

from hajiri.gateway import runtime
from hajiri.gateway.app import Gateway
from hajiri.gateway.archive import RawArchive
from hajiri.gateway.jsutil import iso_ms
from hajiri.gateway.punch import Signer
from hajiri.gateway.wsgi import GATEWAY_MIDDLEWARE, GATEWAY_URLCONF

SIGNER = Signer.ephemeral_key()


class Resp:
    def __init__(self, r):
        self.status = r.status_code
        self.content = r.content
        self.text = r.content.decode('utf-8')
        self.headers = r

    def json(self):
        return json.loads(self.text)


class GwClient:
    """request(app) for one configured Gateway."""

    def __init__(self, gw: Gateway):
        self.gw = gw
        self.c = Client()

    def get(self, path: str, headers: dict | None = None) -> Resp:
        runtime.configure(self.gw)
        return Resp(self.c.get(path, headers=headers or {}))

    def post(self, path: str, body: str | bytes | dict = b'', content_type: str = 'text/plain',
             headers: dict | None = None) -> Resp:
        runtime.configure(self.gw)
        if isinstance(body, dict):
            body, content_type = json.dumps(body), 'application/json'
        data = body.encode('utf-8') if isinstance(body, str) else body
        return Resp(self.c.generic('POST', path, data, content_type=content_type, headers=headers or {}))


@pytest.fixture(autouse=True)
def gateway_settings(settings):
    settings.ROOT_URLCONF = GATEWAY_URLCONF
    settings.MIDDLEWARE = GATEWAY_MIDDLEWARE
    settings.ALLOWED_HOSTS = ['*']
    yield
    runtime.configure(None)


@pytest.fixture
def make_app():
    """make_app(store, signer=..., cipher=..., archive=..., max_clock_skew_min=...) -> GwClient (createGatewayApp)."""
    def make(store, signer: Signer = SIGNER, archive: RawArchive | None = None, **kw) -> GwClient:
        gw = Gateway(store=store, signer=signer, archive=archive or RawArchive(None), **kw)
        return GwClient(gw)
    return make


def npt_now(offset_ms: int = 0) -> str:
    """Current Nepal wall-clock time as a device would print it."""
    return iso_ms(int(time.time() * 1000) + 345 * 60_000 + offset_ms)[:19].replace('T', ' ')
