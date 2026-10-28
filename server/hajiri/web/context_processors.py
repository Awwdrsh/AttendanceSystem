"""Template context for every page: the signed-in user, the navigation (built lazily), toasts carried by redirect."""
from __future__ import annotations

from django.conf import settings
from django.db.models import F
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

    def my_companies():
        if not (user and user.multi_company):
            return []
        from hajiri.core.companies import my_companies as load
        return load(user)

    def active_company():
        """The company the page works in (name and code are always shown in the layout)."""
        if not (user and user.company_id):
            return None
        ref = user.active_company
        if ref is not None:
            return ref
        from hajiri.db.models import Company
        return (Company.objects.filter(pk=user.company_id).values('code', name=F('name_en')).first()) or None

    def group_link():
        if not user:
            return False
        from hajiri.core.companies import can_open_group
        return can_open_group(user)

    return {
        'hj_user': user,
        # The company switcher (only for people who work in more than one company of a group).
        'hj_companies': SimpleLazyObject(my_companies),
        'hj_company': SimpleLazyObject(lambda: _Opt(active_company())),
        # The Group hub: people with a group role or figures of several companies, and admins (who can add a sister
        # company).
        'hj_group_link': SimpleLazyObject(lambda: _Bool(group_link())),
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


class _Opt:
    """An optional object SimpleLazyObject can wrap: falsy when there is none, attributes (or keys) otherwise."""
    def __init__(self, v):
        self.v = v

    def __bool__(self):
        return self.v is not None

    def __getattr__(self, name):
        v = self.__dict__['v']
        if isinstance(v, dict):
            try:
                return v[name]
            except KeyError:
                raise AttributeError(name) from None
        return getattr(v, name)


class _Str:
    """An optional string SimpleLazyObject can wrap: falsy when there is none."""
    def __init__(self, v: str | None):
        self.v = v or ''

    def __bool__(self):
        return bool(self.v)

    def __str__(self):
        return self.v
