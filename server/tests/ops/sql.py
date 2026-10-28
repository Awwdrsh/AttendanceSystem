"""Plain SQL for the ops tests' checks: they state what the seeders must leave in the tables independently of the ORM
code under test (a check written with the same querysets would share its mistakes)."""
import json

from django.db import connection

JSON_TYPES = {114, 3802}        # json, jsonb: Django's connection hands them back as text


def rows(sql: str, params=()) -> list[dict]:
    with connection.cursor() as cur:
        cur.execute(sql, list(params))
        if cur.description is None:
            return []
        cols = [(c.name, c.type_code in JSON_TYPES) for c in cur.description]
        return [{n: json.loads(v) if is_json and isinstance(v, str) else v for (n, is_json), v in zip(cols, r)}
                for r in cur.fetchall()]


def one(sql: str, params=()) -> dict | None:
    found = rows(sql, params)
    return found[0] if found else None


def execute(sql: str, params=()) -> None:
    with connection.cursor() as cur:
        cur.execute(sql, list(params))


def count(sql: str, params=()) -> int:
    return one(f'SELECT count(*)::int AS n FROM ({sql}) x', params)['n']
