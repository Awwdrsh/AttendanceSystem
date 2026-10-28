"""OpenAPI schema (/api/schema) and Swagger UI (/api/docs) for the JSON API, for admins only."""
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from hajiri.core.api import HasRole

# The Swagger UI page loads its script and styles from jsDelivr; only this page's policy allows that.
DOCS_CSP = ("default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' "
            "https://cdn.jsdelivr.net; img-src 'self' data: https://cdn.jsdelivr.net; font-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'")


class SchemaView(SpectacularAPIView):
    permission_classes = [HasRole('admin')]


class DocsView(SpectacularSwaggerView):
    permission_classes = [HasRole('admin')]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Content-Security-Policy'] = DOCS_CSP
        return response
