"""
The payroll rule for the per-day divisor named an internal planning note as its source ("plan.md §6.10/§11.3"); the
Rules page showed it to HR as a broken link. It now says what it is: common practice, not set by law.
"""
from django.db import migrations

OLD = 'plan.md §6.10/§11.3 (practice, not law)'
NEW = 'Common payroll practice in Nepal (not set by law)'


class Migration(migrations.Migration):
    dependencies = [('hajiri', '0005_model_state_fixes')]

    operations = [
        migrations.RunSQL(
            [("UPDATE public.rule_version SET source_url = %s WHERE source_url = %s", [NEW, OLD])],
            [("UPDATE public.rule_version SET source_url = %s WHERE source_url = %s", [OLD, NEW])],
        ),
    ]
