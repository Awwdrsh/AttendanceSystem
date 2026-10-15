-- Performance and learning: goals with key results and check-ins, a KPI library, a competency framework
-- (knowledge, skill, attitude), review cycles with self, manager and peer reviews, calibration and release, a manager's
-- journal, continuous feedback, 1-on-1s, and a learning catalogue with sessions, enrolments, assessments and certificates.

-- ---------------------------------------------------------------- goals
CREATE TABLE IF NOT EXISTS perf_goal (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id   uuid NOT NULL REFERENCES company(id),
  scope        text NOT NULL CHECK (scope IN ('company', 'department', 'individual')),
  org_unit_id  uuid REFERENCES org_unit(id),
  employee_id  uuid REFERENCES employee(id),
  parent_id    uuid REFERENCES perf_goal(id) ON DELETE SET NULL,
  title        text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  description  text,
  weight       numeric(5,2) NOT NULL DEFAULT 0 CHECK (weight BETWEEN 0 AND 100),
  start_date   date,
  due_date     date,
  status       text NOT NULL DEFAULT 'active' CHECK (status IN ('draft', 'active', 'completed', 'cancelled')),
  progress     numeric(5,2) NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  created_by   uuid REFERENCES app_user(id),
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CHECK (scope <> 'individual' OR employee_id IS NOT NULL),
  CHECK (scope <> 'department' OR org_unit_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS perf_goal_company_idx ON perf_goal (company_id, scope);
CREATE INDEX IF NOT EXISTS perf_goal_employee_idx ON perf_goal (employee_id);

CREATE TABLE IF NOT EXISTS perf_key_result (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  goal_id       uuid NOT NULL REFERENCES perf_goal(id) ON DELETE CASCADE,
  title         text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  measure       text NOT NULL DEFAULT 'number' CHECK (measure IN ('number', 'percent', 'currency', 'boolean')),
  start_value   numeric(16,2) NOT NULL DEFAULT 0,
  target_value  numeric(16,2) NOT NULL DEFAULT 100,
  current_value numeric(16,2) NOT NULL DEFAULT 0,
  unit          text,
  weight        numeric(6,2) NOT NULL DEFAULT 1 CHECK (weight > 0),
  sort          smallint NOT NULL DEFAULT 0,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS perf_key_result_goal_idx ON perf_key_result (goal_id);

CREATE TABLE IF NOT EXISTS perf_checkin (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  goal_id       uuid NOT NULL REFERENCES perf_goal(id) ON DELETE CASCADE,
  key_result_id uuid REFERENCES perf_key_result(id) ON DELETE SET NULL,
  author_id     uuid REFERENCES app_user(id),
  value         numeric(16,2),
  progress      numeric(5,2),
  confidence    smallint CHECK (confidence BETWEEN 1 AND 3),
  comment       text,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS perf_checkin_goal_idx ON perf_checkin (goal_id, created_at);

CREATE TABLE IF NOT EXISTS perf_kpi (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id     uuid NOT NULL REFERENCES company(id),
  designation    text,
  title          text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  description    text,
  measure        text NOT NULL DEFAULT 'number' CHECK (measure IN ('number', 'percent', 'currency', 'boolean')),
  default_target numeric(16,2) NOT NULL DEFAULT 100,
  unit           text,
  active         boolean NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS perf_kpi_company_idx ON perf_kpi (company_id, designation);

-- ---------------------------------------------------------------- competencies (knowledge, skill, attitude)
CREATE TABLE IF NOT EXISTS perf_competency (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id   uuid NOT NULL REFERENCES company(id),
  kind         text NOT NULL CHECK (kind IN ('knowledge', 'skill', 'attitude')),
  name         text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  description  text,
  -- [{ "level": 1..5, "text": "behaviour expected at this level" }]
  indicators   jsonb NOT NULL DEFAULT '[]',
  designations text[] NOT NULL DEFAULT '{}',   -- empty = every designation
  grades       text[] NOT NULL DEFAULT '{}',   -- job levels; empty = every grade
  active       boolean NOT NULL DEFAULT true,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, name)
);

-- ---------------------------------------------------------------- review cycles
CREATE TABLE IF NOT EXISTS perf_cycle (
  id                uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id        uuid NOT NULL REFERENCES company(id),
  name              text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  period_start      date NOT NULL,
  period_end        date NOT NULL,
  status            text NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'self_review', 'manager_review', 'peer_review', 'calibration', 'released', 'closed')),
  goals_weight      numeric(5,2) NOT NULL DEFAULT 60 CHECK (goals_weight BETWEEN 0 AND 100),
  competency_weight numeric(5,2) NOT NULL DEFAULT 40 CHECK (competency_weight BETWEEN 0 AND 100),
  rating_labels     jsonb NOT NULL DEFAULT '["Needs improvement", "Partly meets", "Meets expectations", "Exceeds expectations", "Outstanding"]',
  peer_enabled      boolean NOT NULL DEFAULT false,
  peer_min          smallint NOT NULL DEFAULT 3 CHECK (peer_min BETWEEN 1 AND 10),
  self_due          date,
  manager_due       date,
  peer_due          date,
  calibration_due   date,
  -- { "orgUnitIds": [], "branchIds": [], "joinedBefore": "YYYY-MM-DD" }
  filter            jsonb NOT NULL DEFAULT '{}',
  created_by        uuid REFERENCES app_user(id),
  created_at        timestamptz NOT NULL DEFAULT now(),
  launched_at       timestamptz,
  released_at       timestamptz,
  CHECK (period_end >= period_start),
  CHECK (goals_weight + competency_weight = 100)
);

CREATE TABLE IF NOT EXISTS perf_review (
  id                   uuid PRIMARY KEY DEFAULT uuidv7(),
  cycle_id             uuid NOT NULL REFERENCES perf_cycle(id) ON DELETE CASCADE,
  company_id           uuid NOT NULL REFERENCES company(id),
  employee_id          uuid NOT NULL REFERENCES employee(id),
  manager_employee_id  uuid REFERENCES employee(id),
  self_summary         text,
  self_submitted_at    timestamptz,
  self_score           numeric(4,2),
  manager_summary      text,
  manager_submitted_at timestamptz,
  manager_score        numeric(4,2),
  -- { "incrementPct": 8, "promotion": false, "pip": false, "trainingCourseIds": [], "notes": "" }
  recommendation       jsonb NOT NULL DEFAULT '{}',
  final_rating         numeric(4,2),
  calibrated_by        uuid REFERENCES app_user(id),
  calibration_reason   text,
  released_at          timestamptz,
  acknowledged_at      timestamptz,
  employee_comment     text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (cycle_id, employee_id)
);
CREATE INDEX IF NOT EXISTS perf_review_employee_idx ON perf_review (employee_id);
CREATE INDEX IF NOT EXISTS perf_review_manager_idx ON perf_review (manager_employee_id);

CREATE TABLE IF NOT EXISTS perf_review_item (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  review_id       uuid NOT NULL REFERENCES perf_review(id) ON DELETE CASCADE,
  item_type       text NOT NULL CHECK (item_type IN ('goal', 'competency')),
  ref_id          uuid NOT NULL,
  title           text NOT NULL,
  detail          text,
  weight          numeric(6,2) NOT NULL DEFAULT 1,
  self_rating     smallint CHECK (self_rating BETWEEN 1 AND 5),
  self_comment    text,
  manager_rating  smallint CHECK (manager_rating BETWEEN 1 AND 5),
  manager_comment text,
  UNIQUE (review_id, item_type, ref_id)
);

CREATE TABLE IF NOT EXISTS perf_peer (
  id                   uuid PRIMARY KEY DEFAULT uuidv7(),
  review_id            uuid NOT NULL REFERENCES perf_review(id) ON DELETE CASCADE,
  reviewer_employee_id uuid NOT NULL REFERENCES employee(id),
  status               text NOT NULL DEFAULT 'nominated' CHECK (status IN ('nominated', 'approved', 'declined', 'submitted')),
  rating               smallint CHECK (rating BETWEEN 1 AND 5),
  strengths            text,
  improvements         text,
  nominated_by         uuid REFERENCES app_user(id),
  submitted_at         timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (review_id, reviewer_employee_id)
);

-- ---------------------------------------------------------------- journal, feedback, 1-on-1s
CREATE TABLE IF NOT EXISTS perf_journal (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id  uuid NOT NULL REFERENCES company(id),
  employee_id uuid NOT NULL REFERENCES employee(id),
  author_id   uuid NOT NULL REFERENCES app_user(id),
  note_date   date NOT NULL,
  tag         text NOT NULL DEFAULT 'neutral' CHECK (tag IN ('positive', 'improvement', 'neutral')),
  note        text NOT NULL CHECK (length(note) BETWEEN 1 AND 4000),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS perf_journal_employee_idx ON perf_journal (employee_id, note_date);

CREATE TABLE IF NOT EXISTS perf_feedback (
  id                    uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id            uuid NOT NULL REFERENCES company(id),
  giver_employee_id     uuid NOT NULL REFERENCES employee(id),
  subject_employee_id   uuid NOT NULL REFERENCES employee(id),
  requester_employee_id uuid REFERENCES employee(id),
  status                text NOT NULL DEFAULT 'given' CHECK (status IN ('requested', 'given', 'declined')),
  prompt                text,
  body                  text,
  private_to_manager    boolean NOT NULL DEFAULT false,
  created_at            timestamptz NOT NULL DEFAULT now(),
  given_at              timestamptz,
  CHECK (giver_employee_id <> subject_employee_id),
  CHECK (status <> 'given' OR body IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS perf_feedback_subject_idx ON perf_feedback (subject_employee_id);
CREATE INDEX IF NOT EXISTS perf_feedback_giver_idx ON perf_feedback (giver_employee_id);

CREATE TABLE IF NOT EXISTS perf_one_on_one (
  id                     uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id             uuid NOT NULL REFERENCES company(id),
  manager_employee_id    uuid NOT NULL REFERENCES employee(id),
  employee_id            uuid NOT NULL REFERENCES employee(id),
  scheduled_at           timestamptz NOT NULL,
  cadence                text NOT NULL DEFAULT 'none' CHECK (cadence IN ('none', 'weekly', 'biweekly', 'monthly')),
  status                 text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'done', 'cancelled')),
  shared_notes           text,
  manager_private_notes  text,
  employee_private_notes text,
  created_by             uuid REFERENCES app_user(id),
  created_at             timestamptz NOT NULL DEFAULT now(),
  CHECK (manager_employee_id <> employee_id)
);
CREATE INDEX IF NOT EXISTS perf_one_on_one_pair_idx ON perf_one_on_one (manager_employee_id, employee_id, scheduled_at);

CREATE TABLE IF NOT EXISTS perf_one_on_one_item (
  id                uuid PRIMARY KEY DEFAULT uuidv7(),
  meeting_id        uuid NOT NULL REFERENCES perf_one_on_one(id) ON DELETE CASCADE,
  kind              text NOT NULL CHECK (kind IN ('agenda', 'action')),
  text              text NOT NULL CHECK (length(text) BETWEEN 1 AND 1000),
  owner_employee_id uuid REFERENCES employee(id),
  done              boolean NOT NULL DEFAULT false,
  added_by          uuid REFERENCES app_user(id),
  created_at        timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- learning
CREATE TABLE IF NOT EXISTS lrn_course (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id      uuid NOT NULL REFERENCES company(id),
  code            text NOT NULL,
  title           text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  description     text,
  category        text,
  delivery        text NOT NULL DEFAULT 'classroom' CHECK (delivery IN ('online', 'classroom', 'blended')),
  source          text NOT NULL DEFAULT 'internal' CHECK (source IN ('internal', 'external')),
  provider        text,
  duration_hours  numeric(6,2),
  url             text,
  cost            numeric(12,2) NOT NULL DEFAULT 0 CHECK (cost >= 0),
  mandatory       boolean NOT NULL DEFAULT false,
  mandatory_for   text[] NOT NULL DEFAULT '{}',   -- designations; empty with mandatory = everyone
  validity_months smallint CHECK (validity_months BETWEEN 1 AND 120),
  pass_mark       smallint CHECK (pass_mark BETWEEN 1 AND 100),
  active          boolean NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, code)
);

CREATE TABLE IF NOT EXISTS lrn_question (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  course_id     uuid NOT NULL REFERENCES lrn_course(id) ON DELETE CASCADE,
  prompt        text NOT NULL,
  options       jsonb NOT NULL,
  correct_index smallint NOT NULL CHECK (correct_index >= 0),
  sort          smallint NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lrn_session (
  id         uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id uuid NOT NULL REFERENCES company(id),
  course_id  uuid NOT NULL REFERENCES lrn_course(id) ON DELETE CASCADE,
  starts_at  timestamptz NOT NULL,
  ends_at    timestamptz NOT NULL,
  venue      text,
  trainer    text,
  capacity   integer CHECK (capacity > 0),
  status     text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'completed', 'cancelled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (ends_at > starts_at)
);

CREATE TABLE IF NOT EXISTS lrn_enrollment (
  id                     uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id             uuid NOT NULL REFERENCES company(id),
  course_id              uuid NOT NULL REFERENCES lrn_course(id),
  session_id             uuid REFERENCES lrn_session(id),
  employee_id            uuid NOT NULL REFERENCES employee(id),
  source                 text NOT NULL CHECK (source IN ('self', 'assigned', 'review', 'mandatory')),
  status                 text NOT NULL CHECK (status IN ('pending_approval', 'enrolled', 'completed', 'rejected', 'cancelled', 'no_show')),
  workflow_instance_id   uuid,
  review_id              uuid REFERENCES perf_review(id) ON DELETE SET NULL,
  reason                 text,
  assigned_by            uuid REFERENCES app_user(id),
  attended               boolean,
  completed_at           timestamptz,
  score                  numeric(5,2),
  passed                 boolean,
  feedback_rating        smallint CHECK (feedback_rating BETWEEN 1 AND 5),
  feedback_comment       text,
  certificate_file_id    uuid REFERENCES stored_file(id),
  certificate_issued_on  date,
  certificate_expires_on date,
  expiry_reminded_at     timestamptz,
  cost                   numeric(12,2) NOT NULL DEFAULT 0,
  created_at             timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS lrn_enrollment_open_idx ON lrn_enrollment (course_id, employee_id) WHERE status IN ('pending_approval', 'enrolled');
CREATE INDEX IF NOT EXISTS lrn_enrollment_employee_idx ON lrn_enrollment (employee_id);
CREATE INDEX IF NOT EXISTS lrn_enrollment_expiry_idx ON lrn_enrollment (certificate_expires_on) WHERE certificate_expires_on IS NOT NULL;

CREATE TABLE IF NOT EXISTS lrn_attempt (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  enrollment_id uuid NOT NULL REFERENCES lrn_enrollment(id) ON DELETE CASCADE,
  answers       jsonb NOT NULL,
  score         numeric(5,2) NOT NULL,
  passed        boolean NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- One row per reminder sent, so a reminder goes out once even if the daily job runs twice.
CREATE TABLE IF NOT EXISTS perf_reminder_sent (
  key        text PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO workflow_definition (company_id, object_type, steps)
SELECT id, 'training_nomination', '[{"role": "manager"}]'::jsonb FROM company
ON CONFLICT (company_id, object_type) DO NOTHING;
