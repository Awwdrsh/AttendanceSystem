"""
Company settings (the company_setting key/value table) through the ORM.

    from hajiri.core.settings_store import get_setting, put_setting, setting_subquery

    policy = get_setting(company_id, 'helpdesk.policy', {})
    put_setting(company_id, 'helpdesk.policy', policy, updated_by=user.id)       # one INSERT ... ON CONFLICT UPDATE

    # a value of each company's setting next to a row, in one query:
    Company.objects.annotate(digest=setting_subquery('notifications.policy', 'digestTime'))
"""
from __future__ import annotations

from typing import Any

from django.db import models
from django.db.models import OuterRef, Subquery
from django.db.models.fields.json import KT

from .clock import now


def _model():
    from hajiri.db.models import CompanySetting
    return CompanySetting


def get_setting(company_id, key: str, default: Any = None) -> Any:
    v = _model().objects.filter(company_id=company_id, key=key).values_list('value', flat=True).first()
    return default if v is None else v


def put_setting(company_id, key: str, value: Any, *, updated_by=None) -> None:
    """Insert or replace one setting (atomic upsert: INSERT ... ON CONFLICT (company_id, key) DO UPDATE)."""
    put_settings([company_id], key, value, updated_by=updated_by)


def put_settings(company_ids, key: str, value: Any, *, updated_by=None) -> None:
    """The same setting for several companies in one statement."""
    CompanySetting = _model()
    at = now()
    rows = [CompanySetting(company_id=c, key=key, value=value, updated_at=at, updated_by_id=updated_by) for c in company_ids]
    if rows:
        fields = ['value', 'updated_at'] + (['updated_by'] if updated_by else [])
        CompanySetting.objects.bulk_create(rows, update_conflicts=True, unique_fields=['company', 'key'],
                                           update_fields=fields)


def add_setting_if_missing(company_id, key: str, value: Any) -> bool:
    """Store the setting unless the company already has one; True when this call added it (race-safe:
    get_or_create retries the read when a parallel insert wins)."""
    _, created = _model().objects.get_or_create(company_id=company_id, key=key, defaults={'value': value})
    return created


def setting_subquery(key: str, path: str | None = None, *, company_ref=OuterRef('company_id'),
                     output_field: models.Field | None = None) -> Subquery:
    """A correlated subquery for one setting of the outer row's company: the whole JSON value, or the text at a key
    path ('digestTime', 'require2faRoles') as `value->>'path'` (cast with output_field, e.g. IntegerField())."""
    qs = _model().objects.filter(company_id=company_ref, key=key)
    if path is None:
        return Subquery(qs.values('value')[:1], output_field=output_field or models.JSONField())
    expr = KT(f'value__{path.replace(".", "__")}')
    if output_field is not None:
        expr = models.functions.Cast(expr, output_field)
    return Subquery(qs.annotate(_v=expr).values('_v')[:1], output_field=output_field or models.TextField())
