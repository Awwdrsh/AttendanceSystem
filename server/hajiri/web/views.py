"""
The web app is the React single-page app in frontend/ (plain JavaScript, Vite). Its production build lives in
server/frontend_build/ (committed, so an install needs only Python): WhiteNoise serves that folder at the site root
(WHITENOISE_ROOT: /assets/*, sw.js, manifest.webmanifest, icons) and `spa` below answers its index.html for every
page address, the way the TS API served apps/web/dist (app.ts: every GET outside /api/ and /health).

Also here: client_ip, shared by the JSON endpoints that record where a request came from.
"""
from __future__ import annotations

import threading
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse, JsonResponse

_index_lock = threading.Lock()
_index_cache: dict[str, tuple[float, bytes]] = {}

NOT_BUILT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Hajiri</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:system-ui,sans-serif;max-width:640px;margin:64px auto;padding:0 16px;line-height:1.5">
<h1 style="font-size:20px">The web app has not been built</h1>
<p>server/frontend_build/index.html is missing. Run <code>scripts/build-frontend.sh</code> (needs Node.js), or restore
the committed build with <code>git checkout -- server/frontend_build</code>.</p></body></html>"""


def client_ip(request) -> str:
    """REMOTE_ADDR, or the first X-Forwarded-For hop when the request came through a proxy on this machine."""
    remote = request.META.get('REMOTE_ADDR') or 'unknown'
    fwd = request.META.get('HTTP_X_FORWARDED_FOR')
    if fwd and remote in ('127.0.0.1', '::1'):
        return fwd.split(',')[0].strip() or remote
    return remote


def index_html() -> bytes | None:
    """frontend_build/index.html, read again only when the file changes (a rebuild while the server runs)."""
    path = Path(settings.FRONTEND_BUILD_DIR) / 'index.html'
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    with _index_lock:
        hit = _index_cache.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
        body = path.read_bytes()
        _index_cache[key] = (mtime, body)
        return body


def spa(request, *args, **kwargs):
    """index.html of the React app for any page address (/, /login, /employees/<id>, /payroll/runs/<id>, ...). React
    Router decides what to show, including its own sign-in redirect and the legacy addresses. Never cached, so a new
    release is picked up on the next navigation; the hashed files it loads are cached for a year. Other methods get
    the TS 404 JSON, as Express answered anything but a GET there."""
    if request.method not in ('GET', 'HEAD'):
        return api_not_found(request)
    if request.path.startswith('/assets/'):
        return build_asset(request, request.path.removeprefix('/'))
    body = index_html()
    if body is None:
        resp = HttpResponse(NOT_BUILT, status=503, content_type='text/html; charset=utf-8')
    else:
        resp = HttpResponse(body, content_type='text/html; charset=utf-8')
    resp['Cache-Control'] = 'no-cache'
    return resp


def build_asset(request, rel: str):
    """A file under /assets/ that WhiteNoise did not index: WhiteNoise lists the build folder when the server starts,
    while index.html is read again after a rebuild, so after `vite build` on a running server the new index.html names
    hashed files WhiteNoise does not know. Serve them from the folder; a missing one is a 404, never index.html (the
    browser refuses an HTML answer to a module script and the app stays blank)."""
    import re

    from django.http import Http404, HttpResponseNotFound
    from django.views.static import serve
    try:
        resp = serve(request, rel, document_root=settings.FRONTEND_BUILD_DIR)
    except Http404:
        return HttpResponseNotFound('not found', content_type='text/plain; charset=utf-8')
    if re.match(settings.WHITENOISE_IMMUTABLE_FILE_TEST, '/' + rel):
        resp['Cache-Control'] = 'max-age=315360000, public, immutable'
    return resp


def api_not_found(request, *args, **kwargs):
    """An /api/ address no endpoint serves: the TS API's final `res.status(404).json({ error: 'not found' })`."""
    return JsonResponse({'error': 'not found'}, status=404)
