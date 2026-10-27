"""URLconf for the device gateway process (port 8080). `manage.py run_gateway` serves it (see hajiri.gateway.wsgi);
it can also be selected with DJANGO_ROOT_URLCONF=config.gateway_urls."""
from django.urls import include, path

urlpatterns = [
    path('', include(('hajiri.gateway.urls', 'gateway'))),
]

# Devices must get plain text, never an HTML error page.
handler404 = 'hajiri.gateway.views.not_found'
handler500 = 'hajiri.gateway.views.server_error'
