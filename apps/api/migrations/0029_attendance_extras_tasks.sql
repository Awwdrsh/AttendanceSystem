-- Leave & attendance extras (ordered and clubbed leave types, late-arrival penalties, blocking late clock-ins, manual
-- attendance upload, attendance issue nudges, who's out) and the Tasks module.

-- ---------------------------------------------------------------- ordered and clubbed leave types
-- sort_order (already on leave_type) is the leave order: when two leaves are separated only by weekly offs or holidays,
-- the non-working days in between are charged to the type that comes first in the order, if that type (or the company)
-- applies the sandwich rule. A type clubbed with another follows that type's order and sandwich rule.
ALTER TABLE leave_type ADD COLUMN IF NOT EXISTS club_with_id uuid REFERENCES leave_type(id) ON DELETE SET NULL;
DO $$ BEGIN
  ALTER TABLE leave_type ADD CONSTRAINT leave_type_club_not_self CHECK (club_with_id IS NULL OR club_with_id <> id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Non-working days between this request and an adjacent leave, charged by the sandwich rule across requests.
ALTER TABLE leave_request ADD COLUMN IF NOT EXISTS bridge_days numeric(5,2) NOT NULL DEFAULT 0;
ALTER TABLE leave_request ADD COLUMN IF NOT EXISTS bridge_type_id uuid REFERENCES leave_type(id);
ALTER TABLE leave_request ADD COLUMN IF NOT EXISTS bridge_from date;
ALTER TABLE leave_request ADD COLUMN IF NOT EXISTS bridge_to date;

-- ---------------------------------------------------------------- late-arrival penalties
CREATE TABLE IF NOT EXISTS late_penalty (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id     uuid NOT NULL REFERENCES company(id),
  employee_id    uuid NOT NULL REFERENCES employee(id),
  bs_year        smallint NOT NULL,
  bs_month       smallint NOT NULL CHECK (bs_month BETWEEN 1 AND 12),
  late_days      integer NOT NULL CHECK (late_days >= 0),
  late_minutes   integer NOT NULL DEFAULT 0,
  penalty_days   numeric(5,2) NOT NULL CHECK (penalty_days > 0),
  leave_days     numeric(5,2) NOT NULL DEFAULT 0,
  leave_type_id  uuid REFERENCES leave_type(id),
  unpaid_days    numeric(5,2) NOT NULL DEFAULT 0,
  rule           jsonb NOT NULL DEFAULT '{}',
  status         text NOT NULL DEFAULT 'applied' CHECK (status IN ('applied', 'reversed')),
  applied_by     uuid REFERENCES app_user(id),
  applied_at     timestamptz NOT NULL DEFAULT now(),
  reversed_by    uuid REFERENCES app_user(id),
  reversed_at    timestamptz,
  reverse_reason text
);
CREATE UNIQUE INDEX IF NOT EXISTS late_penalty_one_per_month ON late_penalty (employee_id, bs_year, bs_month) WHERE status = 'applied';
CREATE INDEX IF NOT EXISTS late_penalty_company_idx ON late_penalty (company_id, bs_year, bs_month);

-- ---------------------------------------------------------------- manual attendance upload
CREATE TABLE IF NOT EXISTS attendance_upload (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id  uuid NOT NULL REFERENCES company(id),
  filename    text,
  reason      text NOT NULL,
  rows_total  integer NOT NULL,
  punches     integer NOT NULL,
  duplicates  integer NOT NULL DEFAULT 0,
  uploaded_by uuid REFERENCES app_user(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- attendance issue nudges (one per target per thing per day)
CREATE TABLE IF NOT EXISTS attendance_nudge (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id     uuid NOT NULL REFERENCES company(id),
  target_user_id uuid NOT NULL REFERENCES app_user(id),
  kind           text NOT NULL CHECK (kind IN ('employee', 'approver')),
  ref            text NOT NULL,
  local_date     date NOT NULL,
  nudged_by      uuid REFERENCES app_user(id),
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (target_user_id, kind, ref, local_date)
);

-- ---------------------------------------------------------------- tasks
CREATE TABLE IF NOT EXISTS task_board (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id    uuid NOT NULL REFERENCES company(id),
  kind          text NOT NULL CHECK (kind IN ('personal', 'team')),
  name          text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  owner_user_id uuid NOT NULL REFERENCES app_user(id),
  columns       jsonb NOT NULL,
  archived_at   timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS task_board_one_personal ON task_board (owner_user_id) WHERE kind = 'personal';
CREATE INDEX IF NOT EXISTS task_board_company_idx ON task_board (company_id);

CREATE TABLE IF NOT EXISTS task_board_member (
  board_id uuid NOT NULL REFERENCES task_board(id) ON DELETE CASCADE,
  user_id  uuid NOT NULL REFERENCES app_user(id),
  added_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (board_id, user_id)
);
CREATE INDEX IF NOT EXISTS task_board_member_user_idx ON task_board_member (user_id);

CREATE TABLE IF NOT EXISTS task (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id   uuid NOT NULL REFERENCES company(id),
  board_id     uuid NOT NULL REFERENCES task_board(id) ON DELETE CASCADE,
  column_id    text NOT NULL,
  title        text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  description  text CHECK (length(description) <= 5000),
  priority     text NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
  due_date     date,
  labels       text[] NOT NULL DEFAULT '{}',
  position     double precision NOT NULL DEFAULT 0,
  created_by   uuid REFERENCES app_user(id),
  completed_at timestamptz,
  archived_at  timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS task_board_idx ON task (board_id, column_id, position);
CREATE INDEX IF NOT EXISTS task_due_idx ON task (company_id, due_date) WHERE completed_at IS NULL AND archived_at IS NULL;

CREATE TABLE IF NOT EXISTS task_assignee (
  task_id uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES app_user(id),
  PRIMARY KEY (task_id, user_id)
);
CREATE INDEX IF NOT EXISTS task_assignee_user_idx ON task_assignee (user_id);

CREATE TABLE IF NOT EXISTS task_checklist_item (
  id       uuid PRIMARY KEY DEFAULT uuidv7(),
  task_id  uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  text     text NOT NULL CHECK (length(text) BETWEEN 1 AND 300),
  done     boolean NOT NULL DEFAULT false,
  position integer NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_comment (
  id         uuid PRIMARY KEY DEFAULT uuidv7(),
  task_id    uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  user_id    uuid NOT NULL REFERENCES app_user(id),
  body       text NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS task_attachment (
  id       uuid PRIMARY KEY DEFAULT uuidv7(),
  task_id  uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  file_id  uuid NOT NULL REFERENCES stored_file(id),
  added_by uuid REFERENCES app_user(id),
  added_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS task_reminder_sent (
  task_id    uuid NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  user_id    uuid NOT NULL REFERENCES app_user(id),
  kind       text NOT NULL,
  local_date date NOT NULL,
  PRIMARY KEY (task_id, user_id, kind, local_date)
);

-- ---------------------------------------------------------------- default policy for existing companies
INSERT INTO company_setting (company_id, key, value)
SELECT c.id, 'attendance.extras', '{"blockLateClockIn": false, "blockAfterMin": 0, "whosOutScope": "team",
  "latePenalty": {"enabled": false, "lateCount": 3, "minLateMinutes": 1, "penaltyDays": 0.5, "leaveTypeIds": []}}'::jsonb
  FROM company c
 WHERE NOT EXISTS (SELECT 1 FROM company_setting s WHERE s.company_id = c.id AND s.key = 'attendance.extras');
