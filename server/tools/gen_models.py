"""Generate hajiri/db/models/*.py from a database built by the TS SQL migrations.

Used once for the initial models and again if the TS schema changes on main before cutover. After cutover the models
are edited by hand and this script is deleted.

    DATABASE_URL=postgres://localhost:5433/hajiri_schema_ref .venv/bin/python tools/gen_models.py

inspectdb output is post-processed with facts from the catalog: array and range column types, column defaults
(as db_default, so ORM inserts can leave them out), and a split of the 180 models into one file per domain.
"""
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import psycopg

SERVER = Path(__file__).resolve().parent.parent
OUT = SERVER / 'hajiri' / 'db' / 'models'

# Table prefix -> models file. First match wins, so longer prefixes come first.
DOMAINS = [
    ('punch_', 'attendance'), ('attendance_', 'attendance'), ('work_break', 'attendance'), ('late_penalty', 'attendance'),
    ('regularisation_', 'attendance'), ('ot_', 'attendance'), ('recompute_queue', 'attendance'), ('field_visit', 'attendance'),
    ('location', 'attendance'), ('registered_device', 'attendance'), ('kiosk', 'attendance'),
    ('device', 'devices'), ('biometric_template', 'devices'),
    ('shift', 'shifts'), ('roster', 'shifts'), ('holiday', 'shifts'), ('rule_version', 'shifts'), ('bs_calendar', 'shifts'),
    ('leave_', 'leave'),
    ('approval_', 'workflow'), ('approver_', 'workflow'), ('workflow_', 'workflow'), ('user_group', 'workflow'),
    ('payroll_', 'payroll'), ('payslip', 'payroll'), ('salary_', 'payroll'), ('loan', 'payroll'), ('tax_', 'payroll'),
    ('bank_file', 'payroll'),
    ('app_user', 'platform'), ('session', 'platform'), ('login_challenge', 'platform'), ('user_recovery_code', 'platform'),
    ('audit_log', 'platform'), ('consent_record', 'platform'), ('notification_outbox', 'platform'),
    ('stored_file', 'platform'), ('company', 'platform'), ('saved_report', 'platform'),
    ('employee', 'people'), ('branch', 'people'), ('designation', 'people'), ('org_unit', 'people'),
    ('career_event', 'people'), ('profile_change_request', 'people'), ('kye_', 'people'),
    ('document_', 'documents'), ('letter_', 'documents'), ('issued_letter', 'documents'), ('policy_', 'documents'),
    ('lifecycle_', 'lifecycle'), ('probation_', 'lifecycle'), ('exit_', 'lifecycle'), ('asset', 'lifecycle'),
    ('disciplinary_', 'relations'), ('grievance', 'relations'), ('relations_', 'relations'), ('helpdesk_', 'relations'),
    ('expense_', 'expenses'), ('travel_', 'expenses'), ('tada_', 'expenses'),
    ('hiring_', 'hiring'),
    ('perf_', 'performance'), ('lrn_', 'performance'),
    ('announcement', 'engage'), ('engage_', 'engage'), ('kudos', 'engage'), ('survey', 'engage'), ('suggestion', 'engage'),
    ('task', 'tasks'),
    ('time_', 'timesheets'), ('timesheet', 'timesheets'),
]

# inspectdb names an FK `<x>_0` when the table also has a plain `<x>` column; give those FKs a real name.
RENAMES = {'designation_0': 'designation_ref'}


def domain(table: str) -> str:
    for prefix, name in DOMAINS:
        if table.startswith(prefix):
            return name
    raise SystemExit(f'no domain for table {table}: add it to DOMAINS')


def db_default(expr: str) -> str | None:
    """Python source for a db_default, or None when the default is not worth mirroring."""
    if expr == 'now()':
        return 'Now()'
    if expr in ('uuidv7()', 'gen_random_uuid()'):
        return f"models.Func(function='{expr[:-2]}', output_field=models.UUIDField())"
    if expr in ('true', 'false'):
        return expr.title()
    if re.fullmatch(r'-?\d+(\.\d+)?', expr):
        return expr
    m = re.fullmatch(r"'(.*)'::(text|jsonb|smallint\[\]|text\[\]|uuid\[\])", expr, re.S)
    if not m:
        return None
    value, kind = m.group(1).replace("''", "'"), m.group(2)
    if kind == 'text':
        return repr(value)
    if kind == 'jsonb':
        return repr(json.loads(value))
    items = [i for i in value.strip('{}').split(',') if i]
    return repr([int(i) for i in items] if kind == 'smallint[]' else items)


def main() -> None:
    url = os.environ['DATABASE_URL']
    with psycopg.connect(url) as conn:
        tables = [r[0] for r in conn.execute(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where nspname = 'public' and c.relkind in ('r', 'p') and not c.relispartition "
            "and c.relname <> 'schema_migrations' order by 1")]
        cols = {(t, c): (udt, d) for t, c, udt, d in conn.execute(
            "select table_name, column_name, udt_name, column_default from information_schema.columns "
            "where table_schema = 'public'")}

    src = subprocess.run(
        [sys.executable, 'manage.py', 'inspectdb', *tables], cwd=SERVER, check=True, capture_output=True, text=True,
        env={**os.environ, 'DATABASE_URL': url}).stdout

    # Split into class blocks and remember which table each model maps to.
    blocks = re.split(r'\n\n\n(?=class )', src.split('\n\n\n', 1)[1])
    model_table: dict[str, str] = {}
    parsed = []
    for block in blocks:
        name = re.match(r'class (\w+)\(', block).group(1)
        table = re.search(r"db_table = '(\w+)'", block).group(1)
        model_table[name] = table
        parsed.append((name, table, block))

    files: dict[str, list[str]] = defaultdict(list)
    for name, table, block in parsed:
        out = []
        for line in block.splitlines():
            m = re.match(r'    (\w+) = models\.(\w+)\((.*)\)(  #.*)?$', line)
            if not m:
                if 'managed = False' not in line:
                    out.append(line)
                continue
            field, kind, args, _comment = m.groups()
            column = re.search(r"db_column='(\w+)'", args)
            column = column.group(1) if column else (field + '_id' if kind in ('ForeignKey', 'OneToOneField') else field)
            udt, default = cols.get((table, column), (None, None))
            field = RENAMES.get(field, field)
            nullable = ', blank=True, null=True' if 'null=True' in args else ''
            if udt and udt.startswith('_'):
                base = {'_text': 'models.TextField()', '_uuid': 'models.UUIDField()', '_int2': 'models.SmallIntegerField()'}[udt]
                kind, args = 'ArrayField', base + nullable
            elif udt == 'daterange':
                kind, args = 'DateRangeField', nullable.lstrip(', ')
            dd = db_default(default) if default else None
            if dd:
                args = f'{args}, db_default={dd}' if args else f'db_default={dd}'
            prefix = '' if kind in ('ArrayField', 'DateRangeField') else 'models.'
            out.append(f'    {field} = {prefix}{kind}({args})')
        files[domain(table)].append('\n'.join(out))

    # Cross-file references need imports; models refer to each other by class name.
    by_file = {name: domain(table) for name, table in model_table.items()}
    OUT.mkdir(parents=True, exist_ok=True)
    for fname, classes in sorted(files.items()):
        body = '\n\n\n'.join(classes)
        defined = {n for n, f in by_file.items() if f == fname}
        used = set(re.findall(r'models\.(?:ForeignKey|OneToOneField)\((\w+),', body)) - defined
        # Quote cross-file targets as 'hajiri.Model' to avoid import cycles.
        for target in used:
            body = re.sub(rf'(models\.(?:ForeignKey|OneToOneField)\(){target},', rf"\1'hajiri.{target}',", body)
        imports = ['from django.db import models', 'from django.db.models.functions import Now']
        if 'ArrayField(' in body or 'DateRangeField(' in body:
            imports.insert(0, 'from django.contrib.postgres.fields import ArrayField, DateRangeField')
        header = (f'"""{fname.title()} tables. Generated by tools/gen_models.py from the SQL schema, then edited by hand.\n\n'
                  'Foreign keys use DO_NOTHING: the database enforces ON DELETE (cascade, set null or restrict) itself.\n"""\n')
        (OUT / f'{fname}.py').write_text(header + '\n'.join(imports) + '\n\n\n' + body.rstrip() + '\n')
    (OUT / '__init__.py').write_text(
        '"""All Hajiri models, one module per domain. Import from here: `from hajiri.db.models import Employee`."""\n'
        + ''.join(f'from .{f} import *  # noqa: F401,F403\n' for f in sorted(files)))
    print(f'{len(parsed)} models in {len(files)} files')


if __name__ == '__main__':
    main()
