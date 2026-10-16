-- Employee relations: grievances heard by a committee, disciplinary cases with configurable stages, acting and
-- deputation assignments, Know Your Employee (KYE) campaigns, sign-in pop-up notices, registered clock-in devices,
-- contract end dates and milestone reminders. Free text in grievances and disciplinary cases is encrypted with the
-- field key and bound to the company and row.

ALTER TABLE employee ADD COLUMN IF NOT EXISTS contract_end date;

-- Temporary and record-only career events. They are logged on the timeline but never change the permanent record.
ALTER TABLE career_event DROP CONSTRAINT IF EXISTS career_event_kind_check;
ALTER TABLE career_event ADD CONSTRAINT career_event_kind_check CHECK (kind IN ('hire', 'confirmation', 'promotion', 'transfer',
  'designation_change', 'grade_change', 'manager_change', 'employment_type_change', 'salary_revision', 'separation', 'rehire', 'note',
  'suspension', 'leave_of_absence', 'acting', 'deputation'));

-- ---------------------------------------------------------------- grievances
CREATE TABLE IF NOT EXISTS grievance_committee_member (
  company_id uuid NOT NULL REFERENCES company(id),
  user_id    uuid NOT NULL REFERENCES app_user(id),
  added_by   uuid REFERENCES app_user(id),
  added_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (company_id, user_id)
);

CREATE TABLE IF NOT EXISTS grievance (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id         uuid NOT NULL REFERENCES company(id),
  number             integer NOT NULL,
  employee_id        uuid NOT NULL REFERENCES employee(id),
  user_id            uuid NOT NULL REFERENCES app_user(id),
  category           text NOT NULL CHECK (category IN ('harassment', 'discrimination', 'safety', 'pay_benefits', 'management', 'policy', 'other')),
  subject_enc        bytea NOT NULL,
  description_enc    bytea NOT NULL,
  concerns_enc       bytea,
  status             text NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted', 'under_review', 'hearing_scheduled', 'decided', 'appealed', 'closed', 'withdrawn')),
  excluded_user_ids  uuid[] NOT NULL DEFAULT '{}',
  hearing_at         timestamptz,
  hearing_place      text,
  outcome            text CHECK (outcome IN ('upheld', 'partly_upheld', 'not_upheld')),
  findings_enc       bytea,
  decided_by         uuid REFERENCES app_user(id),
  decided_at         timestamptz,
  appeal_reason_enc  bytea,
  appealed_at        timestamptz,
  appeal_outcome     text CHECK (appeal_outcome IN ('decision_upheld', 'decision_changed')),
  appeal_note_enc    bytea,
  appeal_decided_by  uuid REFERENCES app_user(id),
  appeal_decided_at  timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, number)
);
CREATE INDEX IF NOT EXISTS grievance_employee_idx ON grievance (employee_id, created_at DESC);

CREATE TABLE IF NOT EXISTS grievance_note (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  grievance_id uuid NOT NULL REFERENCES grievance(id),
  author_id    uuid NOT NULL REFERENCES app_user(id),
  shared       boolean NOT NULL DEFAULT false,
  body_enc     bytea NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS grievance_note_idx ON grievance_note (grievance_id, created_at);

CREATE TABLE IF NOT EXISTS grievance_file (
  grievance_id uuid NOT NULL REFERENCES grievance(id),
  file_id      uuid NOT NULL REFERENCES stored_file(id),
  PRIMARY KEY (grievance_id, file_id)
);

-- One confidential path for grievances: the help desk's "Grievance" topic is retired in favour of the committee flow.
UPDATE helpdesk_category SET active = false WHERE name = 'Grievance' AND confidential AND active;

-- ---------------------------------------------------------------- disciplinary action
CREATE TABLE IF NOT EXISTS disciplinary_stage (
  id                   uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id           uuid NOT NULL REFERENCES company(id),
  code                 text NOT NULL CHECK (code ~ '^[a-z0-9_]{2,40}$'),
  name                 text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  severity             smallint NOT NULL DEFAULT 1 CHECK (severity BETWEEN 1 AND 9),
  effect               text NOT NULL DEFAULT 'none' CHECK (effect IN ('none', 'suspension', 'termination')),
  letter_template_code text,
  sort                 smallint NOT NULL DEFAULT 0,
  active               boolean NOT NULL DEFAULT true,
  UNIQUE (company_id, code)
);

CREATE TABLE IF NOT EXISTS disciplinary_case (
  id                uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id        uuid NOT NULL REFERENCES company(id),
  number            integer NOT NULL,
  employee_id       uuid NOT NULL REFERENCES employee(id),
  category          text NOT NULL CHECK (category IN ('attendance', 'conduct', 'performance', 'policy', 'safety', 'misconduct', 'other')),
  incident_date     date NOT NULL,
  incident_enc      bytea NOT NULL,
  status            text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'show_cause', 'responded', 'decided', 'withdrawn')),
  show_cause_enc    bytea,
  show_cause_at     timestamptz,
  response_due      date,
  response_enc      bytea,
  responded_at      timestamptz,
  stage_id          uuid REFERENCES disciplinary_stage(id),
  decision_note_enc bytea,
  suspension_from   date,
  suspension_to     date,
  exit_id           uuid,
  letter_id         uuid REFERENCES issued_letter(id),
  opened_by         uuid REFERENCES app_user(id),
  decided_by        uuid REFERENCES app_user(id),
  decided_at        timestamptz,
  overdue_notified  boolean NOT NULL DEFAULT false,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, number),
  CHECK (suspension_to IS NULL OR suspension_from IS NULL OR suspension_to >= suspension_from)
);
CREATE INDEX IF NOT EXISTS disciplinary_case_employee_idx ON disciplinary_case (employee_id, created_at DESC);

-- ---------------------------------------------------------------- acting and deputation
CREATE TABLE IF NOT EXISTS employee_temp_assignment (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id     uuid NOT NULL REFERENCES company(id),
  employee_id    uuid NOT NULL REFERENCES employee(id),
  kind           text NOT NULL CHECK (kind IN ('acting', 'deputation')),
  designation    text,
  designation_id uuid REFERENCES designation(id),
  branch_id      uuid REFERENCES branch(id),
  org_unit_id    uuid REFERENCES org_unit(id),
  start_date     date NOT NULL,
  end_date       date NOT NULL,
  note           text,
  status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended', 'cancelled')),
  created_by     uuid REFERENCES app_user(id),
  created_at     timestamptz NOT NULL DEFAULT now(),
  ended_at       timestamptz,
  CHECK (end_date >= start_date)
);
CREATE INDEX IF NOT EXISTS employee_temp_assignment_emp_idx ON employee_temp_assignment (employee_id, start_date);

-- ---------------------------------------------------------------- Know Your Employee
CREATE TABLE IF NOT EXISTS kye_campaign (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id    uuid NOT NULL REFERENCES company(id),
  title         text NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
  message       text,
  sections      text[] NOT NULL,
  audience_kind text NOT NULL DEFAULT 'all' CHECK (audience_kind IN ('all', 'branch', 'department')),
  audience_id   uuid,
  due_date      date NOT NULL,
  status        text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
  reminded      boolean NOT NULL DEFAULT false,
  created_by    uuid REFERENCES app_user(id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  closed_at     timestamptz
);

CREATE TABLE IF NOT EXISTS kye_response (
  campaign_id     uuid NOT NULL REFERENCES kye_campaign(id),
  employee_id     uuid NOT NULL REFERENCES employee(id),
  status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'changes_requested')),
  note            text,
  responded_at    timestamptz,
  last_nudged_at  timestamptz,
  PRIMARY KEY (campaign_id, employee_id)
);

-- ---------------------------------------------------------------- sign-in pop-up notices
ALTER TABLE announcement ADD COLUMN IF NOT EXISTS popup boolean NOT NULL DEFAULT false;
ALTER TABLE announcement ADD COLUMN IF NOT EXISTS popup_until date;
CREATE TABLE IF NOT EXISTS announcement_popup_seen (
  announcement_id uuid NOT NULL REFERENCES announcement(id),
  user_id         uuid NOT NULL REFERENCES app_user(id),
  seen_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (announcement_id, user_id)
);

-- ---------------------------------------------------------------- registered clock-in devices
CREATE TABLE IF NOT EXISTS registered_device (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  company_id   uuid NOT NULL REFERENCES company(id),
  employee_id  uuid NOT NULL REFERENCES employee(id),
  user_id      uuid NOT NULL REFERENCES app_user(id),
  token_hash   text NOT NULL UNIQUE,
  label        text NOT NULL CHECK (length(label) BETWEEN 1 AND 80),
  user_agent   text,
  status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'revoked')),
  requested_at timestamptz NOT NULL DEFAULT now(),
  decided_by   uuid REFERENCES app_user(id),
  decided_at   timestamptz,
  last_used_at timestamptz
);
CREATE INDEX IF NOT EXISTS registered_device_employee_idx ON registered_device (employee_id);

-- ---------------------------------------------------------------- reminders sent once
CREATE TABLE IF NOT EXISTS relations_reminder_sent (
  company_id uuid NOT NULL REFERENCES company(id),
  kind       text NOT NULL,
  ref        text NOT NULL,
  sent_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (company_id, kind, ref)
);

INSERT INTO company_setting (company_id, key, value)
SELECT id, 'relations.policy', '{"retirementAge": 58, "serviceYears": [1, 5, 10, 15, 20, 25, 30], "registeredDevicesOnly": false, "appealDays": 15}'::jsonb FROM company
ON CONFLICT DO NOTHING;
