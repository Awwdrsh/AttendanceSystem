"""
Group-level roles and the sign-in company.

- group_member: a person's roles over a whole group of companies. group_admin is admin in every company of the group
  (and manages group roles), group_hr is HR in every company, group_executive has the read-only executive view in
  every company. Company-level access stays in app_user (home company) and company_membership (other companies).
- company_membership.is_default: the company a fresh sign-in starts in (at most one per person; none: the home company).

Written as SQL like 0003, so the constraints match the 0001 schema's style. Every object is new, so the TS stack keeps
working on the same database.
"""
import django.contrib.postgres.fields
import django.db.models.deletion
import django.db.models.functions.datetime
from django.db import migrations, models

FORWARD = """
CREATE TABLE public.group_member (
    user_id uuid NOT NULL,
    group_id uuid NOT NULL,
    roles text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    CONSTRAINT group_member_pkey PRIMARY KEY (user_id, group_id),
    CONSTRAINT group_member_roles_check CHECK (((cardinality(roles) >= 1) AND (roles <@ ARRAY['group_admin'::text,
        'group_hr'::text, 'group_executive'::text]))),
    CONSTRAINT group_member_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE,
    CONSTRAINT group_member_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.company_group(id) ON DELETE CASCADE,
    CONSTRAINT group_member_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.app_user(id) ON DELETE SET NULL
);
CREATE INDEX group_member_group_id_idx ON public.group_member USING btree (group_id);
ALTER TABLE public.company_membership ADD COLUMN is_default boolean DEFAULT false NOT NULL;
CREATE UNIQUE INDEX company_membership_one_default_idx ON public.company_membership USING btree (user_id) WHERE is_default;
"""

REVERSE = """
DROP INDEX public.company_membership_one_default_idx;
ALTER TABLE public.company_membership DROP COLUMN is_default;
DROP TABLE public.group_member;
"""

GROUP_ROLES = [('group_admin', 'Group admin'), ('group_hr', 'Group HR'), ('group_executive', 'Group executive')]


class Migration(migrations.Migration):

    dependencies = [
        ('hajiri', '0003_company_groups'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunSQL(FORWARD, REVERSE)],
            state_operations=[
                migrations.AddField(
                    model_name='companymembership',
                    name='is_default',
                    field=models.BooleanField(db_default=False),
                ),
                migrations.CreateModel(
                    name='GroupMember',
                    fields=[
                        ('pk', models.CompositePrimaryKey('user_id', 'group_id', blank=True, editable=False, primary_key=True, serialize=False)),
                        ('roles', django.contrib.postgres.fields.ArrayField(base_field=models.TextField(choices=GROUP_ROLES))),
                        ('created_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                        ('created_by', models.ForeignKey(blank=True, db_column='created_by', null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='group_members_granted', to='hajiri.appuser')),
                        ('group', models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, related_name='group_members', to='hajiri.companygroup')),
                        ('user', models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, related_name='group_memberships', to='hajiri.appuser')),
                    ],
                    options={
                        'db_table': 'group_member',
                    },
                ),
            ],
        ),
    ]
