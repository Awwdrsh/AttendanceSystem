"""
Feature switches (port of apps/api/src/modules/features). Each company can switch whole modules off: a switched-off
module disappears from the menu and its pages and endpoints answer 404 "feature off". Everything is on until HR turns
it off, so the setting stores only the keys that are off (a module added later starts switched on).
"""
from __future__ import annotations

import json
import time
from functools import wraps

from django.db import transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.db import execute, one
from hajiri.core.errors import FeatureOffError, ForbiddenError
from hajiri.core.schema import Bool, Obj, Record, parse

FEATURES = (
    {'key': 'time', 'label': 'Time tracking & timesheets', 'description': 'Project timer, weekly timesheets, projects and clients.', 'group': 'Time & attendance'},
    {'key': 'field', 'label': 'Field visits, breaks & kiosks', 'description': 'Field visits with GPS, break tracking and shared clock-in tablets.', 'group': 'Time & attendance'},
    {'key': 'expenses', 'label': 'Expense claims & travel', 'description': 'Expense claims, travel requests, allowances and settlements.', 'group': 'Pay & money'},
    {'key': 'performance', 'label': 'Goals & reviews', 'description': 'Goals and KPIs, review cycles, feedback, 1-on-1s and the manager journal.', 'group': 'Talent'},
    {'key': 'learning', 'label': 'Learning & training', 'description': 'Course catalogue, sessions, training requests, quizzes and certificates.', 'group': 'Talent'},
    {'key': 'hiring', 'label': 'Hiring & careers page', 'description': 'Requisitions, jobs, the public careers page, candidates, interviews and offers.', 'group': 'Talent'},
    {'key': 'lifecycle', 'label': 'Onboarding, exits & assets', 'description': 'Joining checklists, probation reviews, resignations, clearance and company assets.', 'group': 'People'},
    {'key': 'documents', 'label': 'Documents & letters', 'description': 'Employee documents, letter templates and requests, company policies.', 'group': 'People'},
    {'key': 'helpdesk', 'label': 'Help desk', 'description': 'Employee questions and requests to HR, with response targets.', 'group': 'People'},
    {'key': 'tasks', 'label': 'Tasks', 'description': 'Personal and team task boards.', 'group': 'People'},
    {'key': 'kudos', 'label': 'Kudos', 'description': 'Peer appreciation with company values and a leaderboard.', 'group': 'Engagement'},
    {'key': 'surveys', 'label': 'Surveys & polls', 'description': 'Pulse surveys, eNPS and quick polls.', 'group': 'Engagement'},
    {'key': 'suggestions', 'label': 'Suggestion box', 'description': 'Anonymous suggestions to HR.', 'group': 'Engagement'},
)
FEATURE_KEYS: tuple[str, ...] = tuple(f['key'] for f in FEATURES)
FEATURE_SETTING = 'features'

_TTL = 30.0
_cache: dict[str, tuple[float, frozenset[str]]] = {}


def invalidate_features(company_id: str | None = None) -> None:
    if company_id:
        _cache.pop(str(company_id), None)
    else:
        _cache.clear()


def disabled_features(company_id: str) -> frozenset[str]:
    """The switched-off modules of a company (cached for 30 s; a change made here clears the cache at once)."""
    cid = str(company_id)
    hit = _cache.get(cid)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    r = one('SELECT value FROM company_setting WHERE company_id = %s AND key = %s', [cid, FEATURE_SETTING])
    raw = (r or {}).get('value') or {}
    off_raw = raw.get('off') if isinstance(raw, dict) else None
    off = frozenset(k for k in (off_raw if isinstance(off_raw, list) else []) if isinstance(k, str) and k in FEATURE_KEYS)
    _cache[cid] = (time.monotonic(), off)
    return off


def feature_on(company_id: str | None, key: str) -> bool:
    """A user without a company is never gated (as featureGate skipped requests without one)."""
    return not company_id or key not in disabled_features(company_id)


def assert_feature(company_id: str | None, key: str) -> None:
    if not feature_on(company_id, key):
        raise FeatureOffError(key)


def require_feature(key: str):
    """View decorator: 404 "feature off" while the signed-in user's company has the module switched off."""
    def deco(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            user = getattr(request, 'hajiri_user', None)
            assert_feature(user.company_id if user else None, key)
            return view(request, *args, **kwargs)
        return wrapped
    return deco


def view(off: frozenset[str] | set[str]) -> dict:
    return {'features': [{**f, 'enabled': f['key'] not in off} for f in FEATURES],
            'enabled': {k: k not in off for k in FEATURE_KEYS}}


def get_features(user: AuthUser) -> dict:
    """Every signed-in user may read the switches (they drive the menu)."""
    return view(disabled_features(company_id_of(user)))


SET_BODY = Obj({'enabled': Record(Bool(), keys=FEATURE_KEYS)}, strict=True)


@transaction.atomic
def set_features(user: AuthUser, body: dict) -> dict:
    """HR or admin: {'enabled': {key: bool}} for the keys to change; the rest stay as they are."""
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr or admin')
    company_id = company_id_of(user)
    b = parse(SET_BODY, body)
    before = disabled_features(company_id)
    off = set(before)
    for k, on in b['enabled'].items():
        (off.discard if on else off.add)(k)
    value = {'off': [k for k in FEATURE_KEYS if k in off]}
    execute("""INSERT INTO company_setting (company_id, key, value, updated_by) VALUES (%s, %s, %s, %s)
               ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
            [company_id, FEATURE_SETTING, json.dumps(value), user.id])
    invalidate_features(company_id)
    audit('update', 'company_setting', actor_id=user.id, row_id=FEATURE_SETTING,
          before={'off': [k for k in FEATURE_KEYS if k in before]}, after=value)
    transaction.on_commit(lambda: invalidate_features(company_id))
    return view(off)
