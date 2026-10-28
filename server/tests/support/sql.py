"""Plain SQL helpers for test set-up and assertions only. The tests state what must be in the tables independently of
the ORM code under test; application code uses querysets (docs/django-conventions.md)."""
import json
from typing import Any, Sequence

from django.db import connection

_JSON_OIDS = {114, 3802}  # json, jsonb: Django's connection hands them back as text


def rows(sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        names = [c.name for c in cur.description]
        js = [i for i, c in enumerate(cur.description) if c.type_code in _JSON_OIDS]
        out = []
        for r in cur.fetchall():
            if js:
                r = list(r)
                for i in js:
                    if isinstance(r[i], str):
                        r[i] = json.loads(r[i])
            out.append(dict(zip(names, r)))
        return out


def one(sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    r = rows(sql, params)
    return r[0] if r else None


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """Run a statement and return the number of rows it touched."""
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount
