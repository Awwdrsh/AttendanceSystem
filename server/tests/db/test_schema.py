"""The Django models cover the schema built by migration 0001, and the ORM can use it through the db_defaults."""
from datetime import date

import pytest
from django.apps import apps
from django.db import connection
from django.db.backends.postgresql.psycopg_any import DateRange

from hajiri.db.models import Company, Employee, EmployeeAssignment


@pytest.mark.django_db
def test_every_table_has_a_model():
    with connection.cursor() as cur:
        cur.execute("""
            SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
              AND c.relname NOT LIKE 'django\\_%' AND c.relname <> 'schema_migrations'""")
        tables = {r[0] for r in cur.fetchall()}
    modelled = {m._meta.db_table for m in apps.get_app_config('hajiri').get_models()}
    assert tables - modelled == set()
    assert modelled - tables == set()


@pytest.mark.django_db
def test_orm_insert_relies_on_database_defaults():
    company = Company.objects.create(code='ACME', name_en='Acme Pvt. Ltd.')
    assert company.id is not None and company.created_at is not None
    emp = Employee.objects.create(company=company, emp_code='E001', name_en='Sita Sharma')
    emp.refresh_from_db()
    assert emp.weekly_off_days == [6]
    assert emp.skills == []
    EmployeeAssignment.objects.create(employee=emp, valid=DateRange(date(2026, 7, 17), None), employment_type='regular')
    assert EmployeeAssignment.objects.get(employee=emp).valid.lower == date(2026, 7, 17)
