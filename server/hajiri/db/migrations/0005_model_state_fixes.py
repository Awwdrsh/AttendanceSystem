"""
Model state that matches the database, plus two small schema changes for groups of companies.

State only (the database already has these as partial unique indexes from 0001): several links the introspection
read as one-to-one are many-to-one with "only one open at a time" (an open field visit or break, an active travel
settlement, an open asset assignment, a pending probation review, an open exit, a running time entry), and the
timesheet and expense payout keys are unique only for live rows. They are now ForeignKeys with partial
UniqueConstraints of the same names, so validate_unique, ModelForms and the reverse accessors are right.

Database changes:
- employee_exit.kind accepts 'transfer' (a move to a sister company of the group, written by hajiri.group_admin).
- task_board_one_personal is per owner AND company, so a person has a personal board in each company they work in.
Both only widen what the database accepts.
"""
import django.db.models.deletion
from django.db import migrations, models

FORWARD = """
ALTER TABLE public.employee_exit DROP CONSTRAINT employee_exit_kind_check;
ALTER TABLE public.employee_exit ADD CONSTRAINT employee_exit_kind_check CHECK ((kind = ANY (ARRAY['resignation'::text,
    'termination'::text, 'retirement'::text, 'contract_end'::text, 'death'::text, 'other'::text, 'transfer'::text])));
DROP INDEX public.task_board_one_personal;
CREATE UNIQUE INDEX task_board_one_personal ON public.task_board USING btree (owner_user_id, company_id)
    WHERE (kind = 'personal'::text);
"""

BACKWARD = """
DROP INDEX public.task_board_one_personal;
CREATE UNIQUE INDEX task_board_one_personal ON public.task_board USING btree (owner_user_id) WHERE (kind = 'personal'::text);
ALTER TABLE public.employee_exit DROP CONSTRAINT employee_exit_kind_check;
ALTER TABLE public.employee_exit ADD CONSTRAINT employee_exit_kind_check CHECK ((kind = ANY (ARRAY['resignation'::text,
    'termination'::text, 'retirement'::text, 'contract_end'::text, 'death'::text, 'other'::text])));
"""


class Migration(migrations.Migration):

    dependencies = [
        ('hajiri', '0004_group_roles'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunSQL(FORWARD, BACKWARD)],
            state_operations=[
            migrations.AlterUniqueTogether(
                name='expensepayout',
                unique_together=set(),
            ),
            migrations.AlterUniqueTogether(
                name='timesheet',
                unique_together=set(),
            ),
            migrations.AlterField(
                model_name='assetassignment',
                name='asset',
                field=models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, related_name='asset_assignments', to='hajiri.asset'),
            ),
            migrations.AlterField(
                model_name='employeeexit',
                name='employee',
                field=models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, related_name='employee_exits', to='hajiri.employee'),
            ),
            migrations.AlterField(
                model_name='employeeexit',
                name='kind',
                field=models.TextField(choices=[('resignation', 'Resignation'), ('termination', 'Termination'), ('retirement', 'Retirement'), ('contract_end', 'Contract end'), ('death', 'Death'), ('other', 'Other'), ('transfer', 'Transfer to a sister company')]),
            ),
            migrations.AlterField(
                model_name='fieldvisit',
                name='employee',
                field=models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, related_name='field_visits', to='hajiri.employee'),
            ),
            migrations.AlterField(
                model_name='probationreview',
                name='employee',
                field=models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, related_name='probation_reviews', to='hajiri.employee'),
            ),
            migrations.AlterField(
                model_name='taskboard',
                name='owner_user',
                field=models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, related_name='task_boards', to='hajiri.appuser'),
            ),
            migrations.AlterField(
                model_name='timeentry',
                name='employee',
                field=models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, related_name='time_entries', to='hajiri.employee'),
            ),
            migrations.AlterField(
                model_name='travelsettlement',
                name='travel_request',
                field=models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, related_name='travel_settlements', to='hajiri.travelrequest'),
            ),
            migrations.AlterField(
                model_name='workbreak',
                name='employee',
                field=models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, related_name='work_breaks', to='hajiri.employee'),
            ),
            migrations.AddConstraint(
                model_name='assetassignment',
                constraint=models.UniqueConstraint(condition=models.Q(('returned_on__isnull', True)), fields=('asset',), name='asset_assignment_open_idx'),
            ),
            migrations.AddConstraint(
                model_name='employeeexit',
                constraint=models.UniqueConstraint(condition=models.Q(('status__in', ['pending', 'accepted'])), fields=('employee',), name='employee_exit_open_idx'),
            ),
            migrations.AddConstraint(
                model_name='expensepayout',
                constraint=models.UniqueConstraint(condition=models.Q(('parent_payout__isnull', True)), fields=('source_type', 'source_id', 'component_code'), name='expense_payout_source_idx'),
            ),
            migrations.AddConstraint(
                model_name='fieldvisit',
                constraint=models.UniqueConstraint(condition=models.Q(('ended_at__isnull', True)), fields=('employee',), name='field_visit_open_idx'),
            ),
            migrations.AddConstraint(
                model_name='probationreview',
                constraint=models.UniqueConstraint(condition=models.Q(('status', 'pending')), fields=('employee',), name='probation_review_pending_idx'),
            ),
            migrations.AddConstraint(
                model_name='taskboard',
                constraint=models.UniqueConstraint(condition=models.Q(('kind', 'personal')), fields=('owner_user', 'company'), name='task_board_one_personal'),
            ),
            migrations.AddConstraint(
                model_name='timeentry',
                constraint=models.UniqueConstraint(condition=models.Q(('duration_min__isnull', True), ('ended_at__isnull', True)), fields=('employee',), name='time_entry_one_running_idx'),
            ),
            migrations.AddConstraint(
                model_name='timesheet',
                constraint=models.UniqueConstraint(condition=models.Q(('status__in', ['submitted', 'approved'])), fields=('employee', 'week_start'), name='timesheet_live_week_idx'),
            ),
            migrations.AddConstraint(
                model_name='travelsettlement',
                constraint=models.UniqueConstraint(condition=models.Q(('status__in', ['pending', 'approved'])), fields=('travel_request',), name='travel_settlement_active_idx'),
            ),
            migrations.AddConstraint(
                model_name='workbreak',
                constraint=models.UniqueConstraint(condition=models.Q(('ended_at__isnull', True)), fields=('employee',), name='work_break_open_idx'),
            ),
            ],
        ),
    ]
