"""Django views for the device gateway process. Each view turns the request into a GwRequest, runs the Gateway logic
and answers in plain text (JSON only for /health), exactly as the TS gateway did. Served by config.gateway_urls with
no middleware (see hajiri.gateway.wsgi), so there is no CSRF, session or HTML error page on the device VLAN."""
from __future__ import annotations

import codecs
import json
import re
import zlib

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from .app import BodyTooLarge, BodyUnreadable, GwRequest, GwResponse, plain
from .runtime import current

MB = 1024 * 1024
#: Body limits per route prefix, as the TS body parsers were mounted.
LIMIT_FDATA = 10 * MB
LIMIT_ICLOCK = 2 * MB
LIMIT_HIKVISION = 10 * MB


def _decompress(data: bytes, encoding: str, limit: int) -> bytes:
    enc = encoding.strip().lower()
    if enc in ('', 'identity'):
        return data
    if enc in ('gzip', 'x-gzip', 'deflate'):
        wbits = 16 + zlib.MAX_WBITS if enc != 'deflate' else zlib.MAX_WBITS
        try:
            d = zlib.decompressobj(wbits)
            out = d.decompress(data, limit + 1)
        except zlib.error:
            if enc != 'deflate':
                raise BodyUnreadable('invalid gzip body')
            try:   # raw deflate without the zlib header
                d = zlib.decompressobj(-zlib.MAX_WBITS)
                out = d.decompress(data, limit + 1)
            except zlib.error:
                raise BodyUnreadable('invalid deflate body')
        if len(out) > limit:
            raise BodyTooLarge()
        return out
    raise BodyUnreadable(f'unsupported content encoding "{enc}"')


def _read_body(request, limit: int) -> bytes:
    try:
        length = int(request.META.get('CONTENT_LENGTH') or 0)
    except ValueError:
        length = 0
    if length > limit:
        raise BodyTooLarge()
    data = request.read(limit + 1) if length else b''
    if len(data) > limit:
        raise BodyTooLarge()
    return _decompress(data, request.META.get('HTTP_CONTENT_ENCODING', ''), limit)


def _charset(content_type: str) -> str:
    m = re.search(r';\s*charset\s*=\s*"?([^";\s]+)"?', content_type, re.IGNORECASE)
    return m.group(1).lower() if m else 'utf-8'


def _decode_text(data: bytes, content_type: str) -> str:
    cs = _charset(content_type)
    try:
        codecs.lookup(cs)
    except LookupError:
        raise BodyUnreadable(f'unsupported charset "{cs.upper()}"')
    return data.decode(cs, 'replace')


def _gw_request(request, body: bytes = b'', text: str = '') -> GwRequest:
    headers = {k.lower(): v for k, v in request.headers.items()}
    qs = request.META.get('QUERY_STRING', '')
    return GwRequest(method=request.method, path=request.path, url=request.path + (f'?{qs}' if qs else ''),
                     query={k: request.GET.getlist(k) for k in request.GET.keys()}, headers=headers, body=body, text=text)


def _http(resp: GwResponse, method: str) -> HttpResponse:
    body = resp.body.encode('utf-8') if isinstance(resp.body, str) else resp.body
    r = HttpResponse(b'' if method == 'HEAD' else body, status=resp.status, content_type=resp.content_type)
    r['Content-Length'] = str(len(body))
    return r


def _not_found(request) -> GwResponse:
    return plain(f'Cannot {request.method} {request.path}', 404)


def _route(methods: dict[str, str], body: str | None = None, limit: int = 0):
    """A device route: `methods` maps HTTP method to the Gateway handler name; `body` is 'text' or 'raw'."""

    @csrf_exempt
    def view(request):
        method = 'GET' if request.method == 'HEAD' else request.method
        gw = current()
        req = _gw_request(request)
        try:
            handler = methods.get(method)
            if handler is None:
                resp = _not_found(request)
            else:
                if body:
                    data = _read_body(request, limit)
                    req.body = data
                    if body == 'text':
                        req.text = _decode_text(data, request.META.get('CONTENT_TYPE', ''))
                resp = getattr(gw, handler)(req)
        except Exception as exc:  # noqa: BLE001 - devices get plain text whatever went wrong
            resp = gw.error_response(exc, request.path)
        gw.log_request(req, resp.status)
        return _http(resp, request.method)

    return view


cdata = _route({'GET': 'cdata_get', 'POST': 'cdata_post'}, 'text', LIMIT_ICLOCK)
getrequest = _route({'GET': 'getrequest'}, 'text', LIMIT_ICLOCK)
devicecmd = _route({'POST': 'devicecmd'}, 'text', LIMIT_ICLOCK)
fdata = _route({'POST': 'fdata'}, 'text', LIMIT_FDATA)
hikvision_event = _route({'POST': 'hikvision_event'}, 'raw', LIMIT_HIKVISION)


@csrf_exempt
def health(request):
    if request.method not in ('GET', 'HEAD'):
        return _http(_not_found(request), request.method)
    body = json.dumps(current().health(), separators=(',', ':'))
    return _http(GwResponse(200, body, 'application/json; charset=utf-8'), request.method)


@csrf_exempt
def public_key(request):
    gw = current()
    if request.method not in ('GET', 'HEAD'):
        resp = _not_found(request)
    else:
        resp = GwResponse(200, gw.signer.public_key_pem)
    req = _gw_request(request)
    gw.log_request(req, resp.status)
    return _http(resp, request.method)


def not_found(request, exception=None):
    """handler404 for the gateway URLconf: plain text, never an HTML page."""
    resp = _not_found(request)
    try:
        current().log_request(_gw_request(request), 404)
    except Exception:  # noqa: BLE001
        pass
    return _http(resp, request.method)


def server_error(request):
    return _http(plain('ERROR', 500), request.method)
