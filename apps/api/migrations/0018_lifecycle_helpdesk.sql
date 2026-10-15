-- Employee lifecycle: onboarding and exit checklists, probation reviews and confirmations, resignations and other
-- exits with clearance and a confidential exit interview, and a register of company assets held by employees.
-- Help desk: employee questions and requests routed to an owner with priorities, SLA due times and satisfaction ratings.

ALTER TABLE employee ADD COLUMN IF NOT EXISTS confirmed_on date;

-- ---------------------------------------------------------------- checklists (onboarding and exit clearance)
CREATE TABLE IF NOT EXISTS lifecycle_template (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id   uuid NOT NULL REFERENCES company(id),
  kind         text NOT NULL CHECK (kind IN ('onboarding', 'offboarding')),
  name         text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  description  text,
  branch_id    uuid REFERENCES branch(id),
  org_unit_id  uuid REFERENCES org_unit(id),
  is_default   boolean NOT NULL DEFAULT false,
  active       boolean NOT NULL DEFAULT true,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, kind, name)
);

CREATE TABLE IF NOT EXISTS lifecycle_template_task (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  template_id      uuid NOT NULL REFERENCES lifecycle_template(id) ON DELETE CASCADE,
  sort             smallint NOT NULL DEFAULT 0,
  title            text NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
  description      text,
  department       text,
  assignee_type    text NOT NULL CHECK (assignee_type IN ('hr', 'manager', 'employee', 'user')),
  assignee_user_id uuid REFERENCES app_user(id),
  due_offset_days  smallint NOT NULL DEFAULT 0 CHECK (due_offset_days BETWEEN -90 AND 365),
  link             text
);
CREATE INDEX IF NOT EXISTS lifecycle_template_task_idx ON lifecycle_template_task (template_id, sort);

CREATE TABLE IF NOT EXISTS lifecycle_case (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id   uuid NOT NULL REFERENCES company(id),
  employee_id  uuid NOT NULL REFERENCES employee(id),
  kind         text NOT NULL CHECK (kind IN ('onboarding', 'offboarding')),
  template_id  uuid REFERENCES lifecycle_template(id),
  anchor_date  date NOT NULL,
  status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed', 'cancelled')),
  exit_id      uuid,
  started_by   uuid REFERENCES app_user(id),
  created_at   timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS lifecycle_case_open_idx ON lifecycle_case (employee_id, kind) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS lifecycle_case_company_idx ON lifecycle_case (company_id, kind, status);

CREATE TABLE IF NOT EXISTS lifecycle_task (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  case_id          uuid NOT NULL REFERENCES lifecycle_case(id) ON DELETE CASCADE,
  company_id       uuid NOT NULL REFERENCES company(id),
  employee_id      uuid NOT NULL REFERENCES employee(id),
  sort             smallint NOT NULL DEFAULT 0,
  title            text NOT NULL,
  description      text,
  department       text,
  assignee_type    text NOT NULL CHECK (assignee_type IN ('hr', 'manager', 'employee', 'user')),
  assignee_user_id uuid REFERENCES app_user(id),
  due_date         date,
  link             text,
  status           text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'skipped')),
  note             text,
  done_by          uuid REFERENCES app_user(id),
  done_at          timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lifecycle_task_case_idx ON lifecycle_task (case_id, sort);
CREATE INDEX IF NOT EXISTS lifecycle_task_open_idx ON lifecycle_task (company_id, due_date) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS lifecycle_task_comment (
  id         uuid PRIMARY KEY DEFAULT uuidv7(),
  task_id    uuid NOT NULL REFERENCES lifecycle_task(id) ON DELETE CASCADE,
  author_id  uuid NOT NULL REFERENCES app_user(id),
  body       text NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
  created_at timestamptz NOT NULL DEFAULT now()
);

-- One row per reminder already sent, so the daily jobs never repeat themselves.
CREATE TABLE IF NOT EXISTS lifecycle_reminder (
  company_id uuid NOT NULL REFERENCES company(id),
  kind       text NOT NULL,
  ref_id     uuid NOT NULL,
  key        text NOT NULL,
  sent_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, ref_id, key)
);

-- ---------------------------------------------------------------- probation
CREATE TABLE IF NOT EXISTS probation_review (
  id                uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id        uuid NOT NULL REFERENCES company(id),
  employee_id       uuid NOT NULL REFERENCES employee(id),
  probation_end     date,
  rating            smallint NOT NULL CHECK (rating BETWEEN 1 AND 5),
  recommendation    text NOT NULL CHECK (recommendation IN ('confirm', 'extend', 'terminate')),
  extend_to         date,
  last_working_day  date,
  comments          text NOT NULL CHECK (length(comments) BETWEEN 1 AND 4000),
  submitted_by      uuid NOT NULL REFERENCES app_user(id),
  status            text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
  decided_at        timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CHECK (recommendation <> 'extend' OR extend_to IS NOT NULL),
  CHECK (recommendation <> 'terminate' OR last_working_day IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS probation_review_pending_idx ON probation_review (employee_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS employee_confirmation (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id   uuid NOT NULL REFERENCES company(id),
  employee_id  uuid NOT NULL REFERENCES employee(id),
  confirmed_on date NOT NULL,
  review_id    uuid REFERENCES probation_review(id),
  created_by   uuid REFERENCES app_user(id),
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS employee_confirmation_emp_idx ON employee_confirmation (employee_id);

-- ---------------------------------------------------------------- exits
CREATE TABLE IF NOT EXISTS employee_exit (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id       uuid NOT NULL REFERENCES company(id),
  employee_id      uuid NOT NULL REFERENCES employee(id),
  kind             text NOT NULL CHECK (kind IN ('resignation', 'termination', 'retirement', 'contract_end', 'death', 'other')),
  voluntary        boolean NOT NULL,
  reason_category  text,
  reason           text,
  submitted_on     date NOT NULL,
  notice_days      smallint NOT NULL DEFAULT 0,
  proposed_lwd     date NOT NULL,
  accepted_lwd     date,
  status           text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected', 'withdrawn', 'cancelled', 'completed')),
  initiated_by     uuid REFERENCES app_user(id),
  decided_at       timestamptz,
  completed_at     timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS employee_exit_open_idx ON employee_exit (employee_id) WHERE status IN ('pending', 'accepted');
CREATE INDEX IF NOT EXISTS employee_exit_company_idx ON employee_exit (company_id, status);

CREATE TABLE IF NOT EXISTS exit_interview_question (
  id         uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id uuid NOT NULL REFERENCES company(id),
  sort       smallint NOT NULL DEFAULT 0,
  question   text NOT NULL CHECK (length(question) BETWEEN 1 AND 300),
  kind       text NOT NULL CHECK (kind IN ('text', 'rating', 'choice')),
  options    jsonb NOT NULL DEFAULT '[]',
  active     boolean NOT NULL DEFAULT true
);

-- Answers are confidential to HR and encrypted with the field key (AAD exit_interview:<exitId>).
CREATE TABLE IF NOT EXISTS exit_interview (
  exit_id      uuid PRIMARY KEY REFERENCES employee_exit(id),
  company_id   uuid NOT NULL REFERENCES company(id),
  answers_enc  bytea NOT NULL,
  by_employee  boolean NOT NULL,
  recorded_by  uuid NOT NULL REFERENCES app_user(id),
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- assets
CREATE TABLE IF NOT EXISTS asset_type (
  id         uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id uuid NOT NULL REFERENCES company(id),
  name       text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  UNIQUE (company_id, name)
);

CREATE TABLE IF NOT EXISTS asset (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id    uuid NOT NULL REFERENCES company(id),
  type_id       uuid REFERENCES asset_type(id),
  tag           text NOT NULL CHECK (length(tag) BETWEEN 1 AND 40),
  name          text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  serial        text,
  condition     text NOT NULL DEFAULT 'good' CHECK (condition IN ('new', 'good', 'fair', 'poor', 'damaged', 'lost')),
  status        text NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'assigned', 'retired')),
  purchase_date date,
  cost          numeric(14,2),
  notes         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, tag)
);

CREATE TABLE IF NOT EXISTS asset_assignment (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  asset_id       uuid NOT NULL REFERENCES asset(id),
  company_id     uuid NOT NULL REFERENCES company(id),
  employee_id    uuid NOT NULL REFERENCES employee(id),
  assigned_on    date NOT NULL,
  returned_on    date,
  condition_out  text,
  condition_in   text,
  notes          text,
  assigned_by    uuid REFERENCES app_user(id),
  returned_by    uuid REFERENCES app_user(id),
  created_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (returned_on IS NULL OR returned_on >= assigned_on)
);
CREATE UNIQUE INDEX IF NOT EXISTS asset_assignment_open_idx ON asset_assignment (asset_id) WHERE returned_on IS NULL;
CREATE INDEX IF NOT EXISTS asset_assignment_emp_idx ON asset_assignment (employee_id) WHERE returned_on IS NULL;

-- ---------------------------------------------------------------- help desk
CREATE TABLE IF NOT EXISTS helpdesk_category (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id    uuid NOT NULL REFERENCES company(id),
  name          text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description   text,
  confidential  boolean NOT NULL DEFAULT false,
  owner_user_id uuid REFERENCES app_user(id),
  sort          smallint NOT NULL DEFAULT 0,
  active        boolean NOT NULL DEFAULT true,
  UNIQUE (company_id, name)
);

CREATE TABLE IF NOT EXISTS helpdesk_ticket (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id         uuid NOT NULL REFERENCES company(id),
  number             integer NOT NULL,
  requester_user_id  uuid NOT NULL REFERENCES app_user(id),
  employee_id        uuid REFERENCES employee(id),
  category_id        uuid NOT NULL REFERENCES helpdesk_category(id),
  subject            text NOT NULL CHECK (length(subject) BETWEEN 1 AND 160),
  priority           text NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
  status             text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'waiting', 'resolved', 'closed')),
  assignee_user_id   uuid REFERENCES app_user(id),
  sla_due_at         timestamptz NOT NULL,
  first_response_at  timestamptz,
  resolved_at        timestamptz,
  closed_at          timestamptz,
  breached_at        timestamptz,
  rating             smallint CHECK (rating BETWEEN 1 AND 5),
  rating_comment     text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, number)
);
CREATE INDEX IF NOT EXISTS helpdesk_ticket_queue_idx ON helpdesk_ticket (company_id, status, sla_due_at);
CREATE INDEX IF NOT EXISTS helpdesk_ticket_requester_idx ON helpdesk_ticket (requester_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS helpdesk_message (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  ticket_id       uuid NOT NULL REFERENCES helpdesk_ticket(id) ON DELETE CASCADE,
  company_id      uuid NOT NULL REFERENCES company(id),
  author_user_id  uuid NOT NULL REFERENCES app_user(id),
  body            text NOT NULL CHECK (length(body) BETWEEN 1 AND 5000),
  internal        boolean NOT NULL DEFAULT false,
  file_ids        uuid[] NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS helpdesk_message_ticket_idx ON helpdesk_message (ticket_id, created_at);

-- ---------------------------------------------------------------- approvals for existing companies
INSERT INTO workflow_definition (company_id, object_type, steps)
SELECT id, 'probation_review', '[{"role":"hr"}]'::jsonb FROM company ON CONFLICT (company_id, object_type) DO NOTHING;
INSERT INTO workflow_definition (company_id, object_type, steps)
SELECT id, 'resignation', '[{"role":"manager"},{"role":"hr"}]'::jsonb FROM company ON CONFLICT (company_id, object_type) DO NOTHING;
