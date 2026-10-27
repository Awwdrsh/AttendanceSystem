"""
Security headers the TS API set with helmet (apps/api/src/app.ts): a strict Content-Security-Policy on every response
(a view may set its own, e.g. a stored file's sandbox policy), nosniff, no framing. Add
'hajiri.core.security.SecurityHeadersMiddleware' to MIDDLEWARE.
"""
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
       "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; form-action 'self'; "
       "base-uri 'self'")


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        res = self.get_response(request)
        res.headers.setdefault('Content-Security-Policy', CSP)
        res.headers.setdefault('X-Content-Type-Options', 'nosniff')
        res.headers.setdefault('X-Frame-Options', 'DENY')
        res.headers.setdefault('Referrer-Policy', 'no-referrer')
        res.headers.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
        return res
