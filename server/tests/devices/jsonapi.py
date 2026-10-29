"""A JSON client for the /api/v1 tests of the devices, biometrics and dashboard endpoints."""
from __future__ import annotations

import json

from django.test import Client


class Api:
    """Bearer requests: api.get(path, query), api.post(path, body). `Api(None)` sends no credentials."""

    def __init__(self, token: str | None):
        self.token = token
        self.c = Client()
        self.h = {'HTTP_AUTHORIZATION': f'Bearer {token}'} if token else {}

    def get(self, path, query=None):
        return self.c.get(path, query or {}, **self.h)

    def post(self, path, body=None):
        return self.c.post(path, json.dumps(body if body is not None else {}), content_type='application/json', **self.h)

    def post_raw(self, path, data='', content_type='application/json'):
        return self.c.post(path, data, content_type=content_type, **self.h)

    def delete(self, path):
        return self.c.delete(path, **self.h)
