"""A signed-in browser tab that records everything that goes wrong while the React app is used."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

# Text the app shows when a page could not load (i18n.js errorGeneric) or crashed.
ERROR_TEXT = re.compile(r'Something went wrong', re.I)

# Visible red alerts and red toasts (Mantine colours them through a CSS variable on the root element).
VISIBLE_ERRORS_JS = """() => {
  const out = [];
  for (const el of document.querySelectorAll('.mantine-Alert-root, .mantine-Notification-root')) {
    const style = el.getAttribute('style') || '';
    if (/red/.test(style)) out.push((el.innerText || '').trim().slice(0, 300));
  }
  const body = document.body ? document.body.innerText : '';
  if (/Something went wrong/i.test(body)) out.push('page says: Something went wrong');
  const root = document.getElementById('root');
  if (root && !root.innerHTML.trim()) out.push('blank page (nothing rendered)');
  return out;
}"""


@dataclass
class Problem:
    where: str
    kind: str
    detail: str

    def __str__(self):
        return f'[{self.where}] {self.kind}: {self.detail}'


@dataclass
class Tab:
    """One browser context + page for one person. `where` labels what is being done, for the problem list."""
    browser: object
    base: str
    label: str
    problems: list[Problem] = field(default_factory=list)
    where: str = 'start'
    inflight: int = 0
    # API answers a page may legitimately get: (status, path regex). Anything else >= 400 is a problem.
    allowed: list[tuple[int, re.Pattern]] = field(default_factory=lambda: [
        # The app asks who is signed in on every load; signed out, the answer is 401 and it shows the login page.
        (401, re.compile(r'^/api/v1/auth/me$')),
    ])

    def __post_init__(self):
        self.context = self.browser.new_context(base_url=self.base, viewport={'width': 1366, 'height': 900},
                                                timezone_id='Asia/Kathmandu', locale='en-GB')
        self.context.set_default_timeout(15000)
        self.page = self.context.new_page()
        p = self.page
        p.on('console', self._console)
        p.on('pageerror', lambda e: self.add('uncaught exception', f'{e}'[:500]))
        p.on('request', self._req_start)
        p.on('requestfinished', self._req_end)
        p.on('requestfailed', self._req_failed)
        p.on('response', self._response)

    # ---- recording
    def add(self, kind: str, detail: str):
        self.problems.append(Problem(self.where, kind, detail))

    def _console(self, msg):
        if msg.type != 'error':
            return
        text = msg.text
        # A failed fetch is logged by the browser too; the response handler records it with its path and body.
        if text.startswith('Failed to load resource'):
            return
        self.add('console error', text[:500])

    def _tracked(self, req) -> bool:
        return req.resource_type in ('fetch', 'xhr', 'script', 'document', 'stylesheet')

    def _req_start(self, req):
        if self._tracked(req):
            self.inflight += 1

    def _req_end(self, req):
        if self._tracked(req):
            self.inflight = max(0, self.inflight - 1)

    def _req_failed(self, req):
        self._req_end(req)
        failure = req.failure or ''
        if 'ERR_ABORTED' in failure or 'cancelled' in failure.lower():
            return  # a navigation or an AbortController cut it short
        if req.url.startswith(self.base):
            self.add('request failed', f'{req.method} {req.url[len(self.base):]} {failure}')

    def _response(self, resp):
        url = resp.url
        if not url.startswith(self.base) or resp.status < 400:
            return
        path = url[len(self.base):]
        for status, rx in self.allowed:
            if resp.status == status and rx.search(path):
                return
        try:
            body = resp.text()[:300]
        except Exception:  # noqa: BLE001 - the body is only for the report
            body = ''
        self.add(f'HTTP {resp.status}', f'{resp.request.method} {path} {body}')

    def allow(self, status: int, path_regex: str):
        self.allowed.append((status, re.compile(path_regex)))

    # ---- driving
    def settle(self, quiet_ms: int = 400, max_s: float = 20.0):
        """Wait until no request has been in flight for quiet_ms (the page has finished loading its data)."""
        deadline = time.monotonic() + max_s
        quiet_since = None
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(100)
            if self.inflight == 0:
                quiet_since = quiet_since or time.monotonic()
                if (time.monotonic() - quiet_since) * 1000 >= quiet_ms:
                    return
            else:
                quiet_since = None
        self.add('timeout', f'requests still running after {max_s}s')

    def check_visible(self):
        for text in self.page.evaluate(VISIBLE_ERRORS_JS):
            self.add('visible error', text.replace('\n', ' | '))

    def sign_in(self, email: str, password: str):
        self.where = f'sign in {email}'
        p = self.page
        p.goto('/login')
        p.locator('input[type=email]').fill(email)
        p.locator('input[type=password]').fill(password)
        p.get_by_role('button', name=re.compile('sign in', re.I)).click()
        p.wait_for_url(lambda u: '/login' not in u, timeout=20000)
        self.settle()
        path = self.path()
        if path.startswith(('/change-password', '/setup-two-step')):
            self.add('sign in', f'sent to {path} instead of the app')
        self.check_visible()

    def path(self) -> str:
        return self.page.url[len(self.base):] or '/'

    def go(self, route: str, *, client_side: bool = True):
        """Open a route the way the app does (history push, so the session and providers stay loaded)."""
        self.where = route
        before = len(self.problems)
        if client_side:
            self.page.evaluate("(r) => { window.history.pushState({}, '', r); window.dispatchEvent(new PopStateEvent('popstate')); }", route)
        else:
            self.page.goto(route)
        self.settle()
        self.check_visible()
        return self.problems[before:]

    def api_get(self, path: str):
        r = self.context.request.get(path)
        return r.json() if r.ok else None

    def close(self):
        self.context.close()
