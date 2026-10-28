"""
HTMX response helpers and toasts.

- `toast(request, response, message, tone='ok', title=None)`: on an HTMX response, adds an `hj:toast` event to HX-Trigger; on a
  normal response (usually a redirect after a form post), stores a one-time flash cookie that the next full page shows.
- `trigger(response, event, detail)`: merges an event into HX-Trigger (several calls add up).
- `hx_redirect(request, url)`: a full-page redirect that works for both HTMX and normal requests.
- `close_modal(response)`: tells the page to close the open modal (`hj:close-modal`).
- `refresh(response, target_id)`: asks every element listening for `hj:refresh-<id>` to reload (e.g. a table).

Tones mirror lib/toast.ts: ok (teal, 4 s), info (blue, 5 s), err (red, 8 s).
"""
from __future__ import annotations

import json
from typing import Any

from django.core import signing
from django.http import HttpResponse, HttpResponseRedirect

FLASH_COOKIE = 'hj_flash'
_SALT = 'hajiri.web.flash'
TONES = ('ok', 'info', 'err', 'warn')


def is_htmx(request) -> bool:
    """An HTMX request (django-htmx's `request.htmx`, set by HtmxMiddleware; header check for bare requests)."""
    details = getattr(request, 'htmx', None)
    if details is not None:
        return bool(details)
    return request.headers.get('HX-Request') == 'true'


def htmx_target(request) -> str | None:
    """Id of the element HTMX will swap into (without '#'), or None."""
    details = getattr(request, 'htmx', None)
    if details is not None:
        return details.target if details else None
    return request.headers.get('HX-Target') if is_htmx(request) else None


def trigger(response: HttpResponse, event: str, detail: Any = None, header: str = 'HX-Trigger') -> HttpResponse:
    raw = response.headers.get(header)
    events: dict[str, Any] = {}
    if raw:
        try:
            events = json.loads(raw)
        except ValueError:
            events = {name.strip(): None for name in raw.split(',') if name.strip()}
    if event == 'hj:toast':
        events.setdefault('hj:toast', {'items': []})
        events['hj:toast']['items'].append(detail)
    else:
        events[event] = detail
    response.headers[header] = json.dumps(events)
    return response


def _toast_item(message: str, tone: str, title: str | None) -> dict:
    if tone not in TONES:
        raise ValueError(f'unknown toast tone {tone!r}')
    return {'message': str(message), 'tone': tone, **({'title': str(title)} if title else {})}


def toast(request, response: HttpResponse, message: str, tone: str = 'ok', title: str | None = None) -> HttpResponse:
    """
    Show a toast. For an HTMX request (that is not answered with a redirect) it travels in HX-Trigger; otherwise it is
    kept in the flash cookie and shown by the next full page, which is what a redirect after a form post needs.
    """
    item = _toast_item(message, tone, title)
    if is_htmx(request) and not isinstance(response, HttpResponseRedirect):
        return trigger(response, 'hj:toast', item)
    return flash(response, message, tone, title)


def flash(response: HttpResponse, message: str, tone: str = 'ok', title: str | None = None) -> HttpResponse:
    items = list(getattr(response, '_hj_flash_items', []))
    items.append(_toast_item(message, tone, title))
    response._hj_flash_items = items
    response.set_cookie(FLASH_COOKIE, signing.dumps(items, salt=_SALT, compress=True), max_age=120, httponly=True,
                        samesite='Lax', path='/')
    return response


def read_flash(request) -> list[dict]:
    raw = request.COOKIES.get(FLASH_COOKIE)
    if not raw:
        return []
    try:
        items = signing.loads(raw, salt=_SALT, max_age=300)
    except signing.BadSignature:
        return []
    return [i for i in items if isinstance(i, dict) and i.get('tone') in TONES] if isinstance(items, list) else []


def hx_redirect(request, url: str) -> HttpResponse:
    """Redirect the whole page, also from an HTMX request (HX-Redirect)."""
    if is_htmx(request):
        r = HttpResponse(status=204)
        r.headers['HX-Redirect'] = url
        return r
    return HttpResponseRedirect(url)


def close_modal(response: HttpResponse) -> HttpResponse:
    return trigger(response, 'hj:close-modal', True)


def refresh(response: HttpResponse, target_id: str) -> HttpResponse:
    return trigger(response, f'hj:refresh-{target_id}', True)


def no_swap(response: HttpResponse) -> HttpResponse:
    response.headers['HX-Reswap'] = 'none'
    return response
