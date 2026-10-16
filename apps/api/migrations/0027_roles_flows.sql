-- Roles, scopes and approval flows.
--  * hj_manages(viewer, target, date): the one definition of "manages": the line manager, the head of the employee's
--    department or of any department above it, or the head of the employee's branch. Team-scoped queries use it.
--  * Specialist HR roles (payroll_admin, attendance_admin, recruiter, executive) live in app_user.roles like the others.
--  * approval_flow: conditional approval chains per request type (the company default stays in workflow_definition).
--  * user_group: named sets of users, used as approvers and as flow conditions.
--  * approver_delegate: an approver hands their approvals to someone else for a date range (leave, travel).
--  * workflow_instance.routing: notes on how a step was routed (approver away, delegated, fell back to HR).

CREATE OR REPLACE FUNCTION hj_manages(viewer uuid, target uuid, on_date date) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT viewer IS NOT NULL AND target IS NOT NULL AND viewer <> target AND EXISTS (
    SELECT 1 FROM employee_assignment a
     WHERE a.employee_id = target AND a.valid @> on_date
       AND (a.manager_id = viewer
            OR EXISTS (SELECT 1 FROM branch b WHERE b.id = a.branch_id AND b.head_employee_id = viewer)
            OR EXISTS (
              WITH RECURSIVE up(id, parent_id, head, depth) AS (
                SELECT o.id, o.parent_id, o.head_employee_id, 1 FROM org_unit o WHERE o.id = a.org_unit_id
                UNION ALL
                SELECT p.id, p.parent_id, p.head_employee_id, up.depth + 1 FROM org_unit p JOIN up ON p.id = up.parent_id WHERE up.depth < 20)
              SELECT 1 FROM up WHERE up.head = viewer)))
$$;

-- True when the employee heads a department or a branch (heads see manager pages even without the manager role).
CREATE OR REPLACE FUNCTION hj_is_head(emp uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT emp IS NOT NULL AND (EXISTS (SELECT 1 FROM org_unit WHERE head_employee_id = emp) OR EXISTS (SELECT 1 FROM branch WHERE head_employee_id = emp))
$$;

CREATE TABLE IF NOT EXISTS user_group (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id  uuid NOT NULL REFERENCES company(id),
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, name)
);

CREATE TABLE IF NOT EXISTS user_group_member (
  group_id uuid NOT NULL REFERENCES user_group(id) ON DELETE CASCADE,
  user_id  uuid NOT NULL REFERENCES app_user(id),
  PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS approval_flow (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id  uuid NOT NULL REFERENCES company(id),
  object_type text NOT NULL,
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
  -- {"employeeIds":[],"groupIds":[],"designationIds":[],"orgUnitIds":[],"branchIds":[]}; an empty list matches anyone
  conditions  jsonb NOT NULL DEFAULT '{}',
  steps       jsonb NOT NULL,
  priority    integer NOT NULL DEFAULT 0,
  active      boolean NOT NULL DEFAULT true,
  created_by  uuid REFERENCES app_user(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS approval_flow_type_idx ON approval_flow (company_id, object_type) WHERE active;

CREATE TABLE IF NOT EXISTS approver_delegate (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id   uuid NOT NULL REFERENCES company(id),
  user_id      uuid NOT NULL REFERENCES app_user(id),
  delegate_id  uuid NOT NULL REFERENCES app_user(id),
  from_date    date NOT NULL,
  to_date      date NOT NULL,
  note         text,
  created_by   uuid REFERENCES app_user(id),
  created_at   timestamptz NOT NULL DEFAULT now(),
  cancelled_at timestamptz,
  CHECK (to_date >= from_date),
  CHECK (user_id <> delegate_id)
);
CREATE INDEX IF NOT EXISTS approver_delegate_user_idx ON approver_delegate (user_id, from_date, to_date) WHERE cancelled_at IS NULL;

ALTER TABLE workflow_instance ADD COLUMN IF NOT EXISTS routing jsonb NOT NULL DEFAULT '[]';
ALTER TABLE workflow_instance ADD COLUMN IF NOT EXISTS amount numeric(14, 2);
ALTER TABLE workflow_instance ADD COLUMN IF NOT EXISTS flow_id uuid;
