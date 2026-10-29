"""The printable-letter response shared by the documents JSON API (api.py: letters/<id>/html)."""
from __future__ import annotations

from django.http import HttpResponse


def letter_response(html: str) -> HttpResponse:
    """A printable letter: its own strict policy (inline styles and data: images only) and never cached."""
    r = HttpResponse(html, content_type='text/html; charset=utf-8')
    r['Cache-Control'] = 'private, no-store'
    r['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'self' data:"
    return r
