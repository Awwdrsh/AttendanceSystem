"""Template context for every page: the signed-in user, the navigation (built lazily), toasts carried by redirect."""
from __future__ import annotations

from django.conf import settings
from django.utils.functional import SimpleLazyObject

from . import nav


OFFLINE_FLUSH_URL_NAME = 'regularisation:offline_flush'
# Layout.tsx shows the relations popup notices on every signed-in page; the relations app serves the fragment.
POPUP_NOTICES_URL_NAME = 'relations:popup_notices'


def layout(request):
    user = getattr(request, 'hajiri_user', None)

    def build():
        return nav.build(user, request.path) if user else None

    navigation = SimpleLazyObject(build)

    def embedded():
        return bool(user and navigation and navigation.embedded)

    return {
        'hj_user': user,
        'hj_nav': navigation,
        'embedded': SimpleLazyObject(lambda: _Bool(embedded())),
        'hj_flash': getattr(request, 'hj_flash', []),
        'debug_mode': settings.DEBUG,
        # Where queued offline clock-ins go once the browser is back online (lib/offlinePunch.ts). The slice that
        # turns them into missed-punch corrections names its URL 'regularisation:offline_flush'.
        'hj_offline_flush_url': SimpleLazyObject(lambda: _Str(nav.url_of(OFFLINE_FLUSH_URL_NAME) if user else None)),
        'hj_popup_notices_url': SimpleLazyObject(lambda: _Str(nav.url_of(POPUP_NOTICES_URL_NAME) if user else None)),
    }


class _Bool:
    """A bool that SimpleLazyObject can wrap (it cannot wrap a plain bool's identity checks in templates)."""
    def __init__(self, v: bool):
        self.v = v

    def __bool__(self):
        return self.v

    def __str__(self):
        return 'true' if self.v else ''


class _Str:
    """An optional string SimpleLazyObject can wrap: falsy when there is none."""
    def __init__(self, v: str | None):
        self.v = v or ''

    def __bool__(self):
        return bool(self.v)

    def __str__(self):
        return self.v
