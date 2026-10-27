"""Append to the hash-chained audit log (plan §12.8). The database trigger computes prev_hash and row_hash."""
import json
from typing import Any

from django.db import connection

_UNSET: Any = object()


def audit(action: str, table: str, *, actor_id: str | None = None, row_id: str | None = None,
          before: Any = _UNSET, after: Any = _UNSET) -> None:
    """Call inside the same transaction as the mutation. Values are stored as jsonb, so key order does not matter."""
    with connection.cursor() as cur:
        cur.execute(
            'INSERT INTO audit_log (actor_id, action, table_name, row_id, before, after) VALUES (%s, %s, %s, %s, %s, %s)',
            [actor_id, action, table, None if row_id is None else str(row_id),
             None if before is _UNSET else json.dumps(before, default=str),
             None if after is _UNSET else json.dumps(after, default=str)])
