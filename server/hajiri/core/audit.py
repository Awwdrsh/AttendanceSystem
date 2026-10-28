"""Append to the hash-chained audit log (plan §12.8). The database trigger computes prev_hash and row_hash."""
import json
from typing import Any

from django.db import models

_UNSET: Any = object()


def _jsonb(value: Any):
    """What the TS code stored: JSON.stringify of the value (here json.dumps with str() for dates, UUIDs, Decimals).
    An explicit None is the JSON null, not SQL NULL."""
    if value is _UNSET:
        return None
    plain = json.loads(json.dumps(value, default=str))
    return models.Value(None, models.JSONField()) if plain is None else plain


def audit(action: str, table: str, *, actor_id: str | None = None, row_id: str | None = None,
          before: Any = _UNSET, after: Any = _UNSET) -> None:
    """Call inside the same transaction as the mutation. Values are stored as jsonb, so key order does not matter."""
    from hajiri.db.models import AuditLog
    # prev_hash and row_hash are filled in by the audit_log_chain trigger.
    AuditLog.objects.create(actor_id=actor_id, action=action, table_name=table,
                            row_id=None if row_id is None else str(row_id),
                            before=_jsonb(before), after=_jsonb(after), prev_hash='', row_hash='')
