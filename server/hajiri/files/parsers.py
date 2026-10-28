"""
A JSON parser with its own body limit, for the one endpoint that takes more than DATA_UPLOAD_MAX_MEMORY_SIZE.

DRF's JSONParser reads `request.body`, so Django's limit (2.5 MB) applies to it. A base64 upload of a 4 MB file is about
5.3 MB of JSON, and the TS API allowed 6 MB on /files (express.json({ limit: '6mb' })). This parser reads the request
stream itself, up to its own limit: more is 413 'request body too large', unreadable JSON 400 'invalid JSON body'.
"""
from __future__ import annotations

import json

from django.conf import settings
from rest_framework.exceptions import ParseError
from rest_framework.parsers import BaseParser

from hajiri.core.errors import HttpError


class BodyTooLarge(HttpError):
    status, default_message = 413, 'request body too large'


class LimitedJSONParser(BaseParser):
    media_type = 'application/json'
    max_bytes = 6 * 1024 * 1024

    def parse(self, stream, media_type=None, parser_context=None):
        ctx = parser_context or {}
        request = ctx.get('request')
        try:
            declared = int(request.META.get('CONTENT_LENGTH') or 0) if request is not None else 0
        except ValueError:
            declared = 0
        if declared > self.max_bytes:
            raise BodyTooLarge()
        raw = stream.read(self.max_bytes + 1) if stream is not None else b''
        if len(raw) > self.max_bytes:
            raise BodyTooLarge()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode(ctx.get('encoding') or settings.DEFAULT_CHARSET))
        except (ValueError, UnicodeDecodeError):
            raise ParseError('invalid JSON body') from None


def json_parser(max_bytes: int) -> type[LimitedJSONParser]:
    """A LimitedJSONParser class with this limit."""
    return type(f'LimitedJSONParser{max_bytes}', (LimitedJSONParser,), {'max_bytes': max_bytes})
