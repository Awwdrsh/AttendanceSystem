-- Time tracking: clients, projects and activities; timer and manual time entries; weekly timesheets approved by the
-- line manager (workflow object 'timesheet'). Entries with a start and end may not overlap for one employee; a running
-- timer has no end yet and counts as open-ended, so nothing can be logged after it starts until it stops.
CREATE TABLE IF NOT EXISTS time_client (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id  uuid NOT NULL REFERENCES company(id),
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  notes       text,
  archived    boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS time_client_name_idx ON time_client (company_id, lower(name));

CREATE TABLE IF NOT EXISTS time_project (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id    uuid NOT NULL REFERENCES company(id),
  client_id     uuid REFERENCES time_client(id),
  code          text NOT NULL CHECK (code ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$'),
  name          text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  color         text NOT NULL DEFAULT '#12773a' CHECK (color ~ '^#[0-9a-fA-F]{6}$'),
  billable      boolean NOT NULL DEFAULT true,
  bill_rate     numeric(12,2) CHECK (bill_rate IS NULL OR bill_rate >= 0),
  budget_hours  numeric(10,2) CHECK (budget_hours IS NULL OR budget_hours > 0),
  all_members   boolean NOT NULL DEFAULT true,
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS time_project_code_idx ON time_project (company_id, upper(code));

CREATE TABLE IF NOT EXISTS time_project_member (
  project_id   uuid NOT NULL REFERENCES time_project(id) ON DELETE CASCADE,
  employee_id  uuid NOT NULL REFERENCES employee(id),
  PRIMARY KEY (project_id, employee_id)
);

CREATE TABLE IF NOT EXISTS time_activity (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id  uuid NOT NULL REFERENCES company(id),
  project_id  uuid REFERENCES time_project(id) ON DELETE CASCADE,
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  archived    boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS time_activity_name_idx ON time_activity (company_id, coalesce(project_id, '00000000-0000-0000-0000-000000000000'::uuid), lower(name));

CREATE TABLE IF NOT EXISTS time_entry (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id    uuid NOT NULL REFERENCES company(id),
  employee_id   uuid NOT NULL REFERENCES employee(id),
  local_date    date NOT NULL,
  started_at    timestamptz,
  ended_at      timestamptz,
  duration_min  integer CHECK (duration_min IS NULL OR duration_min BETWEEN 1 AND 1440),
  project_id    uuid REFERENCES time_project(id),
  activity_id   uuid REFERENCES time_activity(id),
  note          text CHECK (note IS NULL OR length(note) <= 500),
  billable      boolean NOT NULL DEFAULT false,
  source        text NOT NULL CHECK (source IN ('timer', 'manual')),
  auto_stopped  boolean NOT NULL DEFAULT false,
  created_by    uuid REFERENCES app_user(id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  -- A finished entry always has a duration; a running timer has a start and nothing else.
  CHECK ((ended_at IS NULL AND duration_min IS NULL AND started_at IS NOT NULL) OR duration_min IS NOT NULL),
  CHECK (ended_at IS NULL OR started_at IS NULL OR ended_at > started_at),
  CONSTRAINT time_entry_no_overlap EXCLUDE USING gist (
    employee_id WITH =, tstzrange(started_at, coalesce(ended_at, 'infinity'::timestamptz), '[)') WITH &&
  ) WHERE (started_at IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS time_entry_one_running_idx ON time_entry (employee_id) WHERE ended_at IS NULL AND duration_min IS NULL;
CREATE INDEX IF NOT EXISTS time_entry_employee_date_idx ON time_entry (employee_id, local_date);
CREATE INDEX IF NOT EXISTS time_entry_company_date_idx ON time_entry (company_id, local_date);
CREATE INDEX IF NOT EXISTS time_entry_project_idx ON time_entry (project_id);

CREATE TABLE IF NOT EXISTS timesheet (
  id                    uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id            uuid NOT NULL REFERENCES company(id),
  employee_id           uuid NOT NULL REFERENCES employee(id),
  week_start            date NOT NULL CHECK (extract(dow FROM week_start) = 0),
  status                text NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted', 'approved', 'rejected', 'cancelled')),
  total_min             integer NOT NULL DEFAULT 0,
  note                  text CHECK (note IS NULL OR length(note) <= 500),
  submitted_at          timestamptz NOT NULL DEFAULT now(),
  submitted_by          uuid REFERENCES app_user(id),
  decided_at            timestamptz,
  decided_by            uuid REFERENCES app_user(id),
  decision_comment      text,
  workflow_instance_id  uuid REFERENCES workflow_instance(id)
);
-- One live (submitted or approved) timesheet per person and week; returned and withdrawn ones stay as history and a
-- new row is created for each resubmission, so every submission has its own approval trail.
CREATE UNIQUE INDEX IF NOT EXISTS timesheet_live_week_idx ON timesheet (employee_id, week_start) WHERE status IN ('submitted', 'approved');
CREATE INDEX IF NOT EXISTS timesheet_company_week_idx ON timesheet (company_id, week_start);

INSERT INTO workflow_definition (company_id, object_type, steps)
SELECT id, 'timesheet', '[{"role":"manager"}]'::jsonb FROM company
ON CONFLICT (company_id, object_type) DO NOTHING;

INSERT INTO company_setting (company_id, key, value)
SELECT id, 'time.policy', '{"enabled": true, "requireProject": false, "roundingStep": 0, "roundingMode": "nearest", "fridayReminder": true, "autoStopHours": 12}'::jsonb FROM company
ON CONFLICT DO NOTHING;
