"""Every /api/v1 address the React app calls resolves to a Django JSON view that answers that HTTP method.

Reads frontend/src (no Node needed): each get/post/put/patch/del/download('/...') call, each api('METHOD', '/...')
call, each '/api/v1/...' string (file and photo links) and the report specs' paths (pages/hr/ReportsPage.jsx). Template parts like ${id} become a sample id, query
strings and trailing ${qs(...)} helpers are dropped, and a `const E = '/expenses'` prefix in the same file is filled in.
A path that resolves only to the SPA catch-all, or to a view without a handler for the method, fails the test.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.urls import Resolver404, resolve

FRONTEND_SRC = Path(__file__).resolve().parents[3] / 'frontend' / 'src'
METHODS = {'get': 'GET', 'post': 'POST', 'put': 'PUT', 'patch': 'PATCH', 'del': 'DELETE', 'download': 'GET'}
CALL = re.compile(r"\b(?:[\w.]*\.)?(get|post|put|patch|del|download)\(\s*(?=['\"`])")
API_CALL = re.compile(r"\bapi\(\s*'(GET|POST|PUT|PATCH|DELETE)'\s*,\s*(?=['\"`])")
REPORT_SPECS = 'pages/hr/ReportsPage.jsx'
SAMPLES = ('1', '00000000-0000-4000-8000-000000000001', 'abc')
# Template parts whose value picks the endpoint, not an id: the values the pages pass.
NAMED = {
    'view': ('visits', 'breaks'),
    'report': ('tax-deposit', 'retirement-deposit'),
    'name': ('spend', 'advances', 'pending-payment'),
}


def read_literal(src: str, i: int) -> list[tuple[str, str]]:
    """The string literal starting at src[i] as parts: ('text', s) and ('expr', e) for each ${e}."""
    q = src[i]
    if q != '`':
        return [('text', src[i + 1:src.index(q, i + 1)])]
    parts, buf, j = [], '', i + 1
    while src[j] != '`':
        if src.startswith('${', j):
            parts.append(('text', buf))
            buf, depth, j = '', 1, j + 2
            start = j
            while depth:
                depth += {'{': 1, '}': -1}.get(src[j], 0)
                j += 1
            parts.append(('expr', src[start:j - 1].strip()))
            buf = ''
            continue
        buf += src[j]
        j += 1
    parts.append(('text', buf))
    return [p for p in parts if p != ('text', '')]


def to_path(parts: list[tuple[str, str]], consts: dict[str, str]) -> str | None:
    """A path template ('/x/{}/y'), or None when the literal is not an API path. Query strings are dropped."""
    out = ''
    if parts and parts[0][0] == 'expr' and parts[0][1] not in consts:
        return None             # the whole path is a variable (a helper's parameter); its callers are checked
    for kind, val in parts:
        if kind == 'expr' and val in consts:
            kind, val = 'text', consts[val]
        if kind == 'text':
            if '?' in val:
                return out + val.split('?')[0]
            out += val
        elif out and not out.endswith('/'):
            return out          # ${qs(...)} or ${flag ? '?x=1' : ''} glued to the end of a segment
        else:
            out += '{%s}' % (val if re.fullmatch(r'\w+', val) else '')
    return out if out.startswith('/') else None


def frontend_calls() -> set[tuple[str, str, str]]:
    calls = set()
    for f in sorted(FRONTEND_SRC.rglob('*.js*')):
        if '.test.' in f.name or f.suffix not in ('.js', '.jsx'):
            continue
        src = f.read_text()
        consts = dict(re.findall(r"const (\w+) = '(/[^']*)'", src))
        rel = str(f.relative_to(FRONTEND_SRC))
        for m in CALL.finditer(src):
            p = to_path(read_literal(src, m.end()), consts)
            if p:
                calls.add((METHODS[m.group(1)], p, rel))
        for m in API_CALL.finditer(src):
            p = to_path(read_literal(src, m.end()), consts)
            if p:
                calls.add((m.group(1), p, rel))
        # Report specs (pages/hr/ReportsPage.jsx): { path: '/reports/...' } and people('name') read as GET.
        for m in re.finditer(r"\bpath: *(?=['`]/)", src if rel == REPORT_SPECS else ''):
            p = to_path(read_literal(src, m.end()), consts)
            if p:
                calls.add(('GET', p, rel))
        if rel == REPORT_SPECS and (helper := re.search(r"const (\w+) = \(\w+\) => `(/[^`$]*)\$\{\w+\}`", src)):
            for name in re.findall(r"\b%s\('([\w-]+)'\)" % helper.group(1), src):
                calls.add(('GET', helper.group(2) + name, rel))
        for m in re.finditer(r"['\"`](?=/api/v1)", src):
            p = to_path(read_literal(src, m.start()), consts)
            if p and p.startswith('/api/v1/'):
                method = 'POST' if 'method: \'POST\'' in src[m.start():m.start() + 200] else 'GET'
                calls.add((method, p.removeprefix('/api/v1'), rel))
    return calls


def candidates(template: str):
    names = re.findall(r'\{(\w*)\}', template)
    if not names:
        yield template
        return
    for sample in SAMPLES:
        path = template
        for n in names:
            path = path.replace('{%s}' % n, NAMED.get(n, (sample,))[0], 1)
        yield path
    for n in names:                       # every named value, the other parts as ids
        for v in NAMED.get(n, ()):
            yield re.sub(r'\{\w*\}', '1', template.replace('{%s}' % n, v, 1))


def handles(path: str, method: str) -> bool:
    try:
        match = resolve(path)
    except Resolver404:
        return False
    if match.url_name == 'spa':
        return False
    view = match.func
    cls = getattr(view, 'cls', None) or getattr(view, 'view_class', None)
    if cls is None:
        return True                       # a function view; it checks the method itself
    actions = getattr(view, 'actions', None)
    if actions is not None:
        return method.lower() in actions
    return hasattr(cls, method.lower())


def test_frontend_source_is_found():
    calls = frontend_calls()
    assert len(calls) > 500, f'only {len(calls)} API calls found in {FRONTEND_SRC}'


def test_every_frontend_api_call_resolves():
    unresolved = sorted(
        f'{method} /api/v1{tpl}  ({rel})' for method, tpl, rel in frontend_calls()
        if not any(handles('/api/v1' + p, method) for p in candidates(tpl)))
    assert not unresolved, 'React app calls with no Django endpoint:\n' + '\n'.join(unresolved)


@pytest.mark.parametrize('tpl, parts', [
    ("`/payroll/reports/${report}${qs({ bsYear, format: 'csv' })}`", '/payroll/reports/{report}'),
    ("`/employees/import${dryRun ? '?dryRun=true' : ''}`", '/employees/import'),
    ("`${E}/claims/${id}/items`", '/expenses/claims/{id}/items'),
    ("`/field/${view}?from=${from}&to=${to}`", '/field/{view}'),
    ("'/auth/me'", '/auth/me'),
])
def test_path_extraction(tpl, parts):
    assert to_path(read_literal(tpl, 0), {'E': '/expenses'}) == parts
