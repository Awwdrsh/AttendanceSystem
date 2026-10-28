"""Print the URL map as Markdown: the JSON endpoints, the gateway's routes and, with --all, every page.

    server/.venv/bin/python scripts/django_routes.py [--all]

Without --all it lists the JSON endpoints kept for non-browser clients (/api/...) and the device gateway's routes;
with --all it also lists every page and HTMX partial (/_/<app>/...).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / 'server'


def clean(pattern: str) -> str:
    """A regex route like ^(?i:iclock/cdata)/?$ (the gateway's case-insensitive paths) shown as its path."""
    import re
    m = re.fullmatch(r'\^\(\?i:(.*)\)/\?\$', pattern)
    return m.group(1) if m else pattern


def walk(resolver, prefix: str = '', ns: str | None = None):
    from django.urls.resolvers import URLResolver
    for p in resolver.url_patterns:
        if isinstance(p, URLResolver):
            yield from walk(p, prefix + str(p.pattern), p.namespace or ns)
        else:
            cb = p.callback
            target = getattr(cb, '__wrapped__', cb)
            while hasattr(target, '__wrapped__'):
                target = target.__wrapped__
            yield '/' + clean(prefix + str(p.pattern)), ns or '', p.name or '', f'{target.__module__}.{getattr(target, "__qualname__", target.__name__)}'


def render(show_all: bool) -> str:
    sys.path.insert(0, str(SERVER))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from django.urls import get_resolver
    out: list[str] = []
    web = list(walk(get_resolver('config.urls')))
    api = [r for r in web if r[0].startswith('/api/')]
    out.append(f'### JSON endpoints ({len(api)})\n\n| Path | URL name | View |\n|---|---|---|')
    out += [f'| `{p}` | `{ns}:{name}` | `{view}` |' for p, ns, name, view in api]
    gw = list(walk(get_resolver('config.gateway_urls')))
    out.append(f'\n### Device gateway, port 8080 ({len(gw)})\n\n| Path | URL name | View |\n|---|---|---|')
    out += [f'| `{p}` | `{ns}:{name}` | `{view}` |' for p, ns, name, view in gw]
    if show_all:
        rest = [r for r in web if not r[0].startswith('/api/')]
        out.append(f'\n### Pages and partials ({len(rest)})\n\n| Path | URL name | View |\n|---|---|---|')
        out += [f'| `{p}` | `{ns}:{name}` | `{view}` |' for p, ns, name, view in rest]
    return '\n'.join(out) + '\n'


if __name__ == '__main__':
    sys.stdout.write(render('--all' in sys.argv[1:]))
